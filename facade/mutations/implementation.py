import logging

import strawberry
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from facade import inputs, models, types
from facade.descriptors import compile_descriptors_to_jsonpath, compile_returndescriptors_to_jsonpath
from facade.protocol import infer_protocols
from facade.registration_lock import lock_organization
from facade.unique import infer_action_scope
from kante.types import Info
from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel
from rekuest_core import scalars as rscalars
from authentikate.vars import get_user, get_client
from facade.higher_order import validate_dependency_coverage, validate_higher_order_pairing
from facade.provenance import audience as provenance_audience
import typing as t
from facade.catalog_validation import catalogs_for_definition, dump_diagnostics, iter_definition_calls, iter_definition_widgets, validate_calls_against_catalogs, validate_widgets_against_catalogs

logger = logging.getLogger(__name__)


# =========================================================
# 2. THE PORT EXTRACTOR (The Relational Engine Builder)
# =========================================================
def _bulk_create_ports_level_by_level(
    port_datas: t.Sequence[t.Any],
    action: models.Action,
    port_model: type[models.ArgPort] | type[models.ReturnPort],
    descriptor_field: str,  # "requires" (args) | "provides" (returns)
    compiler: t.Callable[[t.Any], str | None],
) -> None:
    """Flatten the pydantic port tree into relational rows, one bulk_create per depth level.

    Children reference their parent row's PK, so each level is built only after the previous
    level's bulk_create (Postgres RETURNING populates the pks) — O(tree depth) INSERT statements
    instead of one per port.
    """

    def build(port_data, parent, index, parent_path):
        # The Semantic Materialized Path (e.g., "options.advanced.mask")
        path = f"{parent_path}.{port_data.key}" if parent_path else port_data.key
        # children might be base PortInputModels without requires/provides
        descriptors = getattr(port_data, descriptor_field, None) or []
        return (
            port_data,
            path,
            port_model(
                action=action,
                parent=parent,
                index=index,
                key=port_data.key,
                key_path=path,
                kind=port_data.kind.value if hasattr(port_data.kind, "value") else port_data.kind,
                identifier=port_data.identifier,
                compiled_jsonpath=compiler(descriptors),
                nullable=port_data.nullable,
                dimension=port_data.dimension,
            ),
        )

    level = [build(port_data, None, index, "") for index, port_data in enumerate(port_datas or [])]
    while level:
        port_model.objects.bulk_create([row for _, _, row in level])
        level = [
            build(child, instance, child_index, path)
            for port_data, path, instance in level
            for child_index, child in enumerate(port_data.children or [])
        ]


def rebuild_relational_ports(action: models.Action, definition: DefinitionInputModel) -> None:
    """Replace the relational ArgPort/ReturnPort rows for ``action`` from its definition.

    Existing rows are deleted first because this runs on every (re)registration; without
    the purge, reconnecting agents would accumulate duplicate ports that the matching layer
    would then double-count. Also refreshes the pre-calculated root-port counts on the Action.
    """
    # CASCADE on the self-referential ``parent`` FK removes nested children too.
    action.arg_ports.all().delete()
    action.return_ports.all().delete()

    _bulk_create_ports_level_by_level(definition.args, action, models.ArgPort, "requires", compile_descriptors_to_jsonpath)
    _bulk_create_ports_level_by_level(definition.returns, action, models.ReturnPort, "provides", compile_returndescriptors_to_jsonpath)

    action.arg_count = len(definition.args or [])
    action.return_count = len(definition.returns or [])
    action.save(update_fields=["arg_count", "return_count"])


def _resolve_test_targets(definition: DefinitionInputModel, agent: models.Agent) -> list[models.Action]:
    """Resolve the definition's ``is_test_for`` targets to Action rows, org-scoped.

    A target is either ``{hash}`` (exact) or ``{app?, key, version?}`` — ``app`` defaults
    to the registering agent's app and omitting ``version`` matches every version, so a
    test declared once keeps covering new versions of its target. Targets that match
    nothing are skipped with a warning: registration order between an app's tests and
    their targets is not guaranteed, and a missing target must not abort the whole
    atomic registration.
    """
    if not definition.is_test_for:
        return []

    target_q = Q()
    for target in definition.is_test_for:
        if target.hash:
            target_q |= Q(hash=target.hash)
        else:
            q = Q(key=target.key)
            q &= Q(app__identifier=target.app) if target.app else Q(app=agent.app)
            if target.version:
                q &= Q(version=target.version)
            target_q |= q

    resolved = list(models.Action.objects.filter(target_q, organization=agent.organization).select_related("app"))

    def matches(target: t.Any, candidate: models.Action) -> bool:
        if target.hash:
            return candidate.hash == target.hash
        if candidate.key != target.key:
            return False
        if target.app:
            if candidate.app.identifier != target.app:
                return False
        elif candidate.app_id != agent.app_id:
            return False
        return target.version is None or candidate.version == target.version

    unmatched = [target for target in definition.is_test_for if not any(matches(target, candidate) for candidate in resolved)]
    if unmatched:
        logger.warning(f"Action {definition.key} declares is_test_for targets that are not registered (skipped): {[t_.model_dump(exclude_none=True) for t_ in unmatched]}")
    return resolved


def _sync_dependencies(implementation: models.Implementation, dependencies: t.Any) -> None:
    # Not to be confused with ``mutations.blok._sync_blok_dependencies``, which syncs a *blok's*
    # dependencies. Both used to be called ``_sync_dependencies`` and both are imported into
    # ``mutations/agent.py``, which is a trap in the registration path.
    """Upsert the implementation's declared dependencies with the full persisted shape.

    Single writer for both the create and update flows — previously the two inline copies
    persisted different subsets (the create path dropped ``state_demands``, the update path
    dropped the instance-count fields).
    """
    for dependency in dependencies or []:
        models.Dependency.objects.update_or_create(
            implementation=implementation,
            key=dependency.key,
            defaults=dict(
                action_demands=[x.model_dump() for x in dependency.action_dependencies] if dependency.action_dependencies else [],
                state_demands=[x.model_dump() for x in dependency.state_dependencies] if dependency.state_dependencies else [],
                app_filter=dependency.app,
                version_filter=dependency.version,
                min_viable_instances=dependency.min_viable_instances,
                max_viable_instances=dependency.max_viable_instances,
                prefered_instances=dependency.prefered_instances,
                auto_resolvable=dependency.auto_resolvable,
            ),
        )


def _relational_state_is_current(action: models.Action, definition: DefinitionInputModel) -> bool:
    """Whether the action's relational port rows already reflect ``definition``.

    Guards the skip-unchanged fast path: actions registered before the relational engine
    existed have stale zero counts (or counts without rows) and must still be rebuilt.
    """
    if action.arg_count != len(definition.args or []) or action.return_count != len(definition.returns or []):
        return False

    need_args = bool(definition.args)
    need_returns = bool(definition.returns)
    if not (need_args or need_returns):
        return True

    # Both existence probes in one statement — this runs per action on every agent
    # reconnect, so the round trips add up across a large fleet.
    has_args, has_returns = (
        models.Action.objects.filter(pk=action.pk)
        .annotate(
            has_args=Exists(models.ArgPort.objects.filter(action_id=OuterRef("pk"))),
            has_returns=Exists(models.ReturnPort.objects.filter(action_id=OuterRef("pk"))),
        )
        .values_list("has_args", "has_returns")
        .first()
    )
    if need_args and not has_args:
        return False
    if need_returns and not has_returns:
        return False
    return True


def _upsert_action(
    definition: DefinitionInputModel,
    agent: models.Agent,
    *,
    scope: t.Any,
    desired_idempotent: bool,
    action_map: dict[tuple[str, str], models.Action] | None,
) -> tuple[models.Action, bool]:
    """Find or create the Action this definition describes; say whether it changed.

    Returns ``(action, definition_changed)``. That flag is the whole reason this is one step: it
    decides whether the expensive derived-state rebuild downstream runs at all, and it used to be a
    local whose meaning had to be carried across seventy lines of unrelated work.

    An Action is shared by every agent of an app+organization, so three cases have to be
    distinguished: the same definition (nothing to do), a changed one from its owner (rewrite), and
    a changed one from somebody else (refuse — otherwise any agent could redefine another app's
    contract).
    """
    hash = definition.unique_hash
    key = definition.key
    version = definition.version
    app = agent.app
    definition_changed = True
    try:
        if action_map is not None:
            action = action_map.get((key, version))
            if action is None:
                raise models.Action.DoesNotExist
        else:
            action = models.Action.objects.get(key=key, version=version, app=app, organization=agent.organization)
        if action.hash != hash:
            # Update flow with a changed definition: check the app matches to prevent malicious updates.
            if action.app == agent.app:
                action.hash = hash
                action.args = [i.model_dump() for i in definition.args]
                action.returns = [i.model_dump() for i in definition.returns]
                action.port_groups = [i.model_dump() for i in definition.port_groups]
                action.stateful = definition.stateful
                action.scope = scope
                action.kind = definition.kind
                action.description = definition.description or "No description"
                action.name = definition.name
                action.save()
            else:
                raise ValueError(f"Action with key {key} and version {version} already exists but has different hash and you are not the owner. Please update the version or key of your action definition.")
        else:
            # Identical hash ⇒ the persisted args/returns JSON and all derived state are
            # already current — the expensive rewrites below can be skipped.
            definition_changed = False
    except models.Action.DoesNotExist:
        # ``get_or_create``, not ``create``: Actions are shared by every agent of the app+org, so
        # two agents of one fleet registering the same NEW action can both miss the lookup — and a
        # bare ``create`` then raises ``IntegrityError`` against the (organization, app, key,
        # version) constraint, aborting an entire registration. ``get_or_create`` runs its insert
        # in a savepoint and re-reads the winner's row instead. The org lock in
        # ``implement_agent`` makes this the backstop rather than the mechanism.
        action, _ = models.Action.objects.get_or_create(
            key=key,
            version=version,
            app=app,
            organization=agent.organization,
            defaults=dict(
                hash=hash,
                description=definition.description or "No description",
                args=[i.model_dump() for i in definition.args],
                scope=scope,
                stateful=definition.stateful,
                pure=definition.pure,
                idempotent=desired_idempotent,
                allow_probe=definition.allow_probe,
                is_dev=definition.is_dev,
                kind=definition.kind,
                port_groups=[i.model_dump() for i in definition.port_groups],
                returns=[i.model_dump() for i in definition.returns],
                name=definition.name,
            ),
        )
        if action_map is not None:
            action_map[(key, version)] = action
    return action, definition_changed


def _validate_qualifiers(definition: DefinitionInputModel, effect: t.Any) -> bool:
    """Check the definition's semantic claims against its effect class; return ``idempotent``.

    The one place where both are visible together, which is why the contradictions are caught here
    rather than on either model alone. Returns the *effective* idempotence: a pure function is
    definitionally idempotent, so it is upgraded rather than rejected — consumers then only ever
    read ``idempotent`` for the retry axis and ``pure`` for replayability.
    """
    if definition.pure and getattr(effect, "value", effect) == "PHYSICAL":
        raise ValueError(f"Action {definition.key} is declared pure but its implementation has a PHYSICAL effect class — a pure action cannot touch the real world.")
    if definition.pure and definition.stateful:
        raise ValueError(f"Action {definition.key} is declared both pure and stateful — a pure action cannot depend on or change state.")
    return definition.idempotent or definition.pure


def _collect_diagnostics(definition: DefinitionInputModel, agent: models.Agent, optimistics: t.Any) -> list[dict[str, t.Any]]:
    """Validate the definition's calls and widgets against its catalogs; return stored diagnostics.

    Effect/validator calls are evaluated client-side against the base catalog plus the UI catalog
    the definition names. An argument mismatch on a *known* operation raises and aborts
    registration; an operation neither catalog provides is kept as a warning instead, so a UI app
    can roll out a new operation without every agent having to be redeployed first.
    """
    catalogs, diagnostics = catalogs_for_definition(definition, agent)
    diagnostics = [
        *validate_calls_against_catalogs(catalogs, iter_definition_calls(definition, optimistics), f"Definition {definition.key}"),
        *validate_widgets_against_catalogs(catalogs, iter_definition_widgets(definition)),
        *diagnostics,
    ]
    for diagnostic in diagnostics:
        logger.warning(diagnostic.message)
    return dump_diagnostics(diagnostics)


@transaction.atomic
def _create_implementation(
    input: ImplementationInputModel,
    agent: models.Agent,
    *,
    action_map: dict[tuple[str, str], models.Action] | None = None,
    implementation_map: dict[str, models.Implementation] | None = None,
) -> models.Implementation:
    """Register one implementation (and its Action) for ``agent``.

    ``action_map`` (keyed ``(key, version)``, scoped to the agent's app + organization) and
    ``implementation_map`` (keyed ``interface``, ``select_related("action")``) are optional
    batch prefetches: ``implement_agent`` passes them so a reconnecting agent with N
    implementations does two lookups total instead of 2·N. Rows created here are inserted
    back into the maps so duplicate keys within one batch resolve like sequential lookups
    would. ``None`` (the default, used by ``create_implementation``) keeps the per-row
    lookup path.
    """
    definition = input.definition
    scope = infer_action_scope(definition)

    desired_idempotent = _validate_qualifiers(definition, input.effect)
    stored_diagnostics = _collect_diagnostics(definition, agent, input.optimistics)

    action, definition_changed = _upsert_action(
        definition,
        agent,
        scope=scope,
        desired_idempotent=desired_idempotent,
        action_map=action_map,
    )

    # Qualifiers are not identity-bearing (deliberately excluded from unique_hash so flipping
    # them doesn't force fleet re-registration) — sync them unconditionally, covering the
    # update path AND the unchanged-hash fast path.
    qualifier_updates = []
    port_groups = [i.model_dump() for i in definition.port_groups]
    for field, desired in (("pure", definition.pure), ("idempotent", desired_idempotent), ("allow_probe", definition.allow_probe), ("is_dev", definition.is_dev), ("kind", definition.kind), ("port_groups", port_groups)):
        if getattr(action, field) != desired:
            setattr(action, field, desired)
            qualifier_updates.append(field)
    if qualifier_updates:
        action.save(update_fields=qualifier_updates)

    if definition_changed or not _relational_state_is_current(action, definition):
        # 2. (Re)build the relational ArgPort/ReturnPort rows (The Micro-Filtering Engine that
        #    the matching layer queries), plus the usages/protocols/collections derived from the
        #    definition. On agent reconnects with an unchanged hash this whole block is skipped —
        #    everything in it is a pure function of the definition. Trade-off: reconnecting no
        #    longer heals manually edited port rows (only count/existence divergence triggers a
        #    rebuild).
        rebuild_relational_ports(action, definition)

        # Derived M2Ms are RECONCILED (.set), not accumulated (.add): protocols feed the
        # matching engine's protocol demands, so a stale row (e.g. an action that stopped
        # being a predicate) must stop matching, mirroring how the port rows are rebuilt.
        action.protocols.set(infer_protocols(definition, action.organization))

        action.is_test_for.set(_resolve_test_targets(definition, agent))

        collections = []
        for collection_name in definition.collections or []:
            c, _ = models.Collection.objects.get_or_create(name=collection_name, defaults=dict(creator=agent.user, organization=agent.organization))
            collections.append(c)
        action.collections.set(collections)

        logger.info(f"Created {action}")
        action.save()

    # Resolve the provenance audience once, at registration: an explicit declaration
    # wins, otherwise derive it from the action's structure ports (e.g. @mikro/image
    # -> mikro). Persisted on the implementation so dispatch never recomputes it.
    if input.provenance_audience is not None:
        resolved_audience = input.provenance_audience
    else:
        resolved_audience = provenance_audience.derive_from_action(action) or None

    if implementation_map is not None:
        implementation = implementation_map.get(input.interface)
    else:
        # (interface, agent) is unique, so first() is get() without the exception dance.
        implementation = models.Implementation.objects.filter(interface=input.interface, agent=agent).first()

    if implementation is not None:
        if implementation.action.pk != action.pk:
            if implementation.action.implementations.count() == 1:
                logger.info("Deleting Action because it has no more implementations")
                implementation.action.delete()

        implementation.action = action
        implementation.params = input.params or {}
        implementation.release = agent.release
        implementation.needs_token = input.needs_token
        implementation.provenance_audience = resolved_audience
        implementation.effect = getattr(input.effect, "value", input.effect)
        implementation.diagnostics = stored_diagnostics
        implementation.save()
    else:
        implementation = models.Implementation.objects.create(
            interface=input.interface,
            release=agent.release,
            action=action,
            agent=agent,
            params=input.params or {},
            needs_token=input.needs_token,
            provenance_audience=resolved_audience,
            effect=getattr(input.effect, "value", input.effect),
            diagnostics=stored_diagnostics,
        )
        if implementation_map is not None:
            implementation_map[input.interface] = implementation

    _sync_dependencies(implementation, input.dependencies)

    if input.manipulates:
        states = models.State.objects.filter(agent=agent, interface__in=input.manipulates)
        implementation.manipulates.set(states)

    if input.tracks:
        implementation.tracks = [t.model_dump() for t in input.tracks]
        implementation.save()

    return implementation


def create_implementation(info: Info, input: inputs.CreateImplementationInput) -> types.Implementation:
    # Same serialization as ``implement_agent``: this path writes the very same org-shared rows.
    lock_organization(info.context.request.organization)
    agent, _ = models.Agent.objects.update_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
        defaults=dict(
            name=f"{info.context.request.client.client_id}",
            release=info.context.request.client.release,
            app=info.context.request.client.release.app,
        ),
    )

    # Same conversion as implement_agent: the port validators live on the pydantic models.
    return _create_implementation(input.to_pydantic().implementation, agent)


@strawberry.input(description="Mark an existing implementation as a higher-order wrapper of a lower implementation.")
class SetHigherOrderInput:
    implementation: strawberry.ID = strawberry.field(description="The wrapper implementation to mark as higher-order.")
    lower_implementation: strawberry.ID = strawberry.field(description="The lower implementation it wraps.")
    config: rscalars.AnyDefault | None = strawberry.field(default=None, description="Projection config: bound params + arg/dependency/return maps (see Implementation.higher_order_config).")


def set_higher_order(info: Info, input: SetHigherOrderInput) -> types.Implementation:
    """Link a wrapper implementation to the lower implementation it wraps, with a projection config.

    Validates the pairing up front: no self-wrap, no nested wrappers, the action kinds must agree,
    and every lower dependency slot must be covered by a bound dep or one of the wrapper's *declared*
    dependencies (so the caller knows what to pass).
    """
    higher = models.Implementation.objects.get(id=input.implementation)
    lower = models.Implementation.objects.get(id=input.lower_implementation)

    if higher.pk == lower.pk:
        raise ValueError("An implementation cannot wrap itself")

    # A higher-order implementation is always bound to the agent that owns the lower
    # implementation it wraps — the wrapper is virtual and its child runs on that agent.
    # Cross-agent wrapping is not supported: register the wrapper on the lower's agent.
    if higher.agent_id != lower.agent_id:
        raise ValueError("A higher-order implementation must be on the same agent as the lower implementation it wraps. Register the wrapper on the lower implementation's agent.")

    config = input.config or {}

    validate_higher_order_pairing(
        higher.action.kind,
        lower.action.kind,
        lower_is_higher_order=lower.higher_order_for_id is not None,
    )
    validate_dependency_coverage(
        config,
        lower_dependency_keys=[d.key for d in lower.dependencies.all()],
        declared_h_dependency_keys=[d.key for d in higher.dependencies.all()],
    )

    higher.higher_order_for = lower
    higher.higher_order_config = config
    higher.save()
    return higher


def delete_implementation(info: Info, input: inputs.DeleteImplementationInput) -> str:
    implementation = models.Implementation.objects.get(id=input.implementation)

    implementation.delete()

    return input.implementation


def pin_implementation(info: Info, input: inputs.PinInput) -> types.Implementation:
    user = info.context.request.user

    agent = models.Implementation.objects.get(id=input.id)
    if input.pin:
        agent.pinned_by.add(user)
    else:
        agent.pinned_by.remove(user)
    agent.save()
    return agent
