"""Blok mutations: create, update, delete, fork.

These write custom bloks only. A provided blok (``app`` set) is its agents' and changes only with
their registrations; a user who wants to change it, or bind other agents to it, forks it.

Every read here is scoped to the caller's organization. ``scoped_get`` raises ``PermissionError``
for a wrong-tenant id so it is indistinguishable from a missing one; deletes use a scoped filter
for the same reason.
"""

from typing import Iterable, cast

from django.db import transaction
from kante.types import Info

from facade import inputs, models, takt, takt_api, types
from facade.catalog_validation import dump_diagnostics, validate_manifest_against_catalog
from facade.types.base import scoped_get
from rekuest_core import enums as renums
from rekuest_core.inputs import models as rimodels


def _sync_blok_dependencies(blok: models.Blok, dependencies: Iterable[rimodels.AgentDependencyInputModel] | None, *, replace: bool) -> list[models.BlokDependency]:
    """Upsert ``BlokDependency`` rows by ``(blok, key)`` from a manifest.

    Writes every declared field (``optional`` included: before this helper only demands and the
    app/version filters were persisted, so ``optional`` could never be honoured downstream).
    With ``replace`` every existing key that is not declared any more is deleted; a
    ``BlokAgentMapping`` pointing at it keeps its row with ``dependency=NULL`` (``SET_NULL``).
    """
    existing = {dep.key: dep for dep in blok.dependencies.all()}
    synced: list[models.BlokDependency] = []
    for declared in dependencies or []:
        dep, _ = models.BlokDependency.objects.update_or_create(
            blok=blok,
            key=declared.key,
            defaults=dict(
                action_demands=[d.model_dump() for d in declared.action_dependencies or []],
                state_demands=[d.model_dump() for d in declared.state_dependencies or []],
                app_filter=declared.app,
                version_filter=declared.version,
                optional=declared.optional,
                description=declared.description,
                auto_resolvable=declared.auto_resolvable,
                min_viable_instances=declared.min_viable_instances,
                max_viable_instances=declared.max_viable_instances,
            ),
        )
        synced.append(dep)
        existing.pop(declared.key, None)

    if replace:
        for stale in existing.values():
            stale.delete()

    return synced


STANDARD_CATALOG_NAME = "orkestrator"
"""The catalog a blok naming none is checked against: the one the Orkestrator app registers.

The client's offline check uses the same name, and takt's ``register_blok`` falls back to it too.
"""


def _catalog_for(info: Info, name: str | None) -> models.UICatalog:
    """The named catalog in the caller's organization, created empty if it does not exist yet."""
    return models.UICatalog.objects.get_or_create(name=name or STANDARD_CATALOG_NAME, organization=info.context.request.organization)[0]


def _custom(info: Info, id: str) -> models.Blok:
    """The caller's blok ``id``, refused when an app provides it."""
    blok = scoped_get(models.Blok, info, id, field="organization")
    if blok.app_id is not None:
        raise ValueError(f"Blok '{blok.name}' is provided by its agents and changes with them; fork it to edit it.")
    return blok


def _parsed(source: str) -> list[rimodels.ComponentNodeInputModel]:
    """The tree a blok's BSX source describes, parsed by takt (the one server-side BSX parser)."""
    answer = takt.call(takt_api.PARSE_BLOK, takt_api.ParseBlokRequest(source=source))
    return [rimodels.ComponentNodeInputModel(**component) for component in answer.components]


@transaction.atomic
def create_blok(info: Info, input: inputs.CreateBlokInput) -> types.Blok:
    """Create or replace a custom blok in the caller's organization: by its name, or by its ``origin`` when it is installed from one."""
    model = input.to_pydantic()
    if model.source is not None:
        components = _parsed(model.source)
        rimodels.check_blok_manifest(components, {dep.key for dep in model.dependencies or []}, None if model.demo_state is None else set(model.demo_state))
        model = model.model_copy(update={"components": components, "source": None})
    organization = info.context.request.organization
    catalog = _catalog_for(info, model.catalog)
    diagnostics = validate_manifest_against_catalog(catalog, model.components, model.context, model.demo_state)

    # ``organization`` is part of the lookup, not the defaults: keyed on name alone this was a
    # global upsert, so any user could overwrite another organization's blok by name.
    # Only a custom blok is ever replaced: one an app provides under the same name is its own row.
    # A blok installed from an origin is found again by it, whatever it is named, and never
    # replaces a blok made by hand or installed from elsewhere under its name.
    if model.origin is not None:
        if models.Blok.objects.filter(organization=organization, name=model.name, app__isnull=True).exclude(origin=model.origin).exists():
            raise ValueError(f"A custom blok named '{model.name}' exists already; rename or delete it to install this one.")
        lookup = dict(origin=model.origin)
        installed = dict(name=model.name, origin_hash=model.origin_hash)
    else:
        lookup = dict(name=model.name)
        installed = {}
    blok, _ = models.Blok.objects.update_or_create(
        **lookup,
        organization=organization,
        app=None,
        defaults=dict(
            **installed,
            components=[c.model_dump() for c in model.components or []],
            description=model.description,
            creator=info.context.request.user,
            catalog=catalog,
            demo_state=model.demo_state or {},
            diagnostics=dump_diagnostics(diagnostics),
            context=model.context.value,
            for_model=model.for_model,
            for_node=model.for_node,
        ),
    )

    # create is an upsert on (name, organization); dependencies a previous registration declared
    # but this one does not are stale and go too.
    _sync_blok_dependencies(blok, model.dependencies, replace=True)

    return cast("types.Blok", blok)


def delete_blok(info: Info, input: inputs.DeleteBlokInput) -> bool:
    """Delete a custom blok in the caller's organization; False when there is none."""
    if models.Blok.objects.filter(id=input.id, organization=info.context.request.organization, app__isnull=False).exists():
        raise ValueError("A provided blok goes when no agent declares it any more; it cannot be deleted by hand.")
    deleted, _ = models.Blok.objects.filter(id=input.id, organization=info.context.request.organization).delete()
    return deleted > 0


@transaction.atomic
def fork_blok(info: Info, input: inputs.ForkBlokInput) -> types.Blok:
    """Copy a provided blok into a custom one, which lives on whatever its agents do."""
    provided = scoped_get(models.Blok, info, input.id, field="organization")
    if provided.app_id is None:
        raise ValueError(f"Blok '{provided.name}' is already custom.")
    name = input.name or provided.name
    if models.Blok.objects.filter(organization=provided.organization, name=name, app__isnull=True).exists():
        raise ValueError(f"A custom blok named '{name}' exists already.")
    blok = models.Blok.objects.create(
        organization=provided.organization,
        name=name,
        description=provided.description,
        creator=info.context.request.user,
        catalog=provided.catalog,
        components=provided.components,
        demo_state=provided.demo_state,
        context=provided.context,
        for_model=provided.for_model,
        for_node=provided.for_node,
        args=provided.args,
        diagnostics=provided.diagnostics,
    )
    for dependency in provided.dependencies.all():
        dependency.pk = None
        dependency.blok = blok
        dependency.save()
    # Rows are written parents first, so in id order every parent is copied before its children.
    copies: dict[int, models.BlokArgPort] = {}
    for port in provided.arg_ports.order_by("id"):
        source_id, parent_id = port.pk, port.parent_id
        port.pk = None
        port.blok = blok
        port.parent = None if parent_id is None else copies[parent_id]
        port.save()
        copies[source_id] = port
    return cast("types.Blok", blok)


@transaction.atomic
def update_blok(info: Info, input: inputs.UpdateBlokInput) -> types.Blok:
    """Partially update a custom blok in the caller's organization."""
    model = input.to_pydantic()
    blok = _custom(info, model.id)

    if model.name is not None:
        blok.name = model.name
    if model.description is not None:
        blok.description = model.description
    if model.source is not None:
        blok.components = [c.model_dump() for c in _parsed(model.source)]
    if model.components is not None:
        blok.components = [c.model_dump() for c in model.components]
    if model.demo_state is not None:
        blok.demo_state = model.demo_state
    if model.catalog is not None:
        blok.catalog = _catalog_for(info, model.catalog)

    # A partial update is validated as the manifest it results in: new components against the
    # dependencies and demo state that will be in force after the update.
    dependency_keys = {dep.key for dep in model.dependencies} if model.dependencies is not None else set(blok.dependencies.values_list("key", flat=True))
    components = [rimodels.ComponentNodeInputModel(**c) for c in blok.components]
    rimodels.check_blok_manifest(components, dependency_keys, set(blok.demo_state or {}))
    blok.diagnostics = dump_diagnostics(validate_manifest_against_catalog(blok.catalog, components, renums.ComponentContext(blok.context), blok.demo_state))

    blok.save()

    if model.dependencies is not None:
        _sync_blok_dependencies(blok, model.dependencies, replace=True)

    return cast("types.Blok", blok)
