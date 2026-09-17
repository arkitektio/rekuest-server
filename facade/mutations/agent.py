import uuid

from django.db import transaction
from kante.types import Info
from facade.mutations.implementation import _create_implementation
import strawberry
from facade import types, models, inputs, scalars, enums, signals
from rekuest_core.inputs.types import BlokImplementationInput, ImplementationInput, LockImplementationInput, StateImplementationInput
from rekuest_core.inputs.models import BlokImplementationInputModel, ImplementationInputModel, StateImplementationInputModel, LockImplementationInputModel
import logging
from facade import types, models, inputs, unique
from pydantic import BaseModel, Field
import kante
from facade.catalog_validation import dump_diagnostics, validate_manifest_against_catalog
from facade.mutations.blok import _sync_blok_dependencies
from facade.registration_lock import lock_organization
from facade.types.base import scoped_get

logger = logging.getLogger(__name__)


@strawberry.input
class AgentInput:
    name: str | None = strawberry.field(
        default=None,
        description="The name of the agent. This is used to identify the agent in the system.",
    )
    kind: enums.AgentKind | None = strawberry.field(
        default=None,
        description="The transport kind of the agent: WEBSOCKET (default) or WEBHOOK (a HookAgent the backend POSTs to).",
    )
    hook_url: str | None = strawberry.field(
        default=None,
        description="For a WEBHOOK agent: the URL the backend POSTs messages (Assign, Cancel, Caller* events) to.",
    )
    hook_url_secret: str | None = strawberry.field(
        default=None,
        description="For a WEBHOOK agent: the shared secret used to HMAC-sign messages in both directions (outbound delivery and POST intake).",
    )


@strawberry.input
class DeleteAgentInput:
    id: strawberry.ID = strawberry.field(description="The ID of the agent to delete. This is used to identify the agent in the system.")


def _register_state(agent: models.Agent, inputstate: StateImplementationInputModel) -> models.State:
    """Upsert one of the agent's states, defaulting the identity fields: ``key`` falls back
    to the interface, ``app_identifier`` to the agent's app identifier."""
    state_definition, _ = models.StateDefinition.objects.update_or_create(
        hash=unique.hash_state_definition(inputstate.definition),
        organization=agent.organization,
        defaults=dict(
            name=inputstate.definition.name,
            ports=[i.model_dump() for i in inputstate.definition.ports],
            description="A state definition",
        ),
    )

    state, _ = models.State.objects.update_or_create(
        interface=inputstate.interface,
        agent=agent,
        defaults=dict(
            definition=state_definition,
            key=inputstate.key or inputstate.interface,
            app_identifier=inputstate.app or agent.app.identifier,
        ),
    )
    return state


def ensure_agent(info: Info, input: AgentInput) -> types.Agent:
    # TODO: Hasch this

    agent, _ = models.Agent.objects.get_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
        defaults=dict(
            name=input.name or f"{info.context.request.client.client_id}",
            app=info.context.request.client.release.app,
            release=info.context.request.client.release,
        ),
    )

    memory_shelve, _ = models.MemoryShelve.objects.get_or_create(
        agent=agent,
        defaults=dict(
            name=f"{str(agent)} memory shelve",
            creator=info.context.request.user,
            organization=agent.organization,
        ),
    )

    for drawer in models.MemoryDrawer.objects.filter(
        shelve=memory_shelve,
    ):
        drawer.delete()

    # Configure the transport (idempotent): a HookAgent declares its kind + endpoint here.
    updated_fields = []
    became_webhook = False
    if input.kind is not None:
        new_kind = getattr(input.kind, "value", input.kind)
        became_webhook = new_kind == enums.AgentKind.WEBHOOK.value and agent.kind != new_kind
        agent.kind = new_kind
        updated_fields.append("kind")
    if input.hook_url is not None:
        agent.hook_url = input.hook_url
        updated_fields.append("hook_url")
    if input.hook_url_secret is not None:
        agent.hook_url_secret = input.hook_url_secret
        updated_fields.append("hook_url_secret")
    if updated_fields:
        agent.save(update_fields=updated_fields)
    if became_webhook:
        transaction.on_commit(lambda: _abandon_socket_queue(agent.pk))

    return agent


def _abandon_socket_queue(agent_pk: int) -> None:
    """An agent left the websocket transport: what was queued for its socket is now unreachable.

    No connection will ever drain those redis lists again, so drop them and mark the agent's
    not-yet-picked-up tasks as "never dispatched" — the pickup watchdog then redelivers their
    Assigns over the webhook. (Queued control frames are covered by the control deadline.)
    The raw frames are deliberately not re-POSTed: their order is gone and their tokens may be stale.
    """
    from facade.consumers.agent_queue import RedisAgentQueue

    try:
        dropped = RedisAgentQueue.from_settings().drop(str(agent_pk))
    except Exception:
        logger.error("Could not drop the socket queue of agent %s", agent_pk, exc_info=True)
        dropped = 0
    models.Task.objects.filter(agent_id=agent_pk, is_done=False, picked_up_at__isnull=True).update(dispatched_at=None)
    if dropped:
        logger.warning("Agent %s became a HookAgent: dropped %s frame(s) queued for its socket", agent_pk, dropped)


class ImplementAgentInputModel(BaseModel):
    name: str | None = Field(default=None, description="The name of the agent. This is used to identify the agent in the system.")
    states: list[StateImplementationInputModel] | None = Field(default=None, description="The states of the agent. This is used to specify the initial states of the agent")
    implementations: list[ImplementationInputModel] | None = Field(default=None, description="The implementations of the agent. This is used to specify the initial implementations of the agent")
    locks: list[LockImplementationInputModel] | None = Field(default=None, description="The locks of the agent. This is used to specify which resources the agent needs to run")
    bloks: list[BlokImplementationInputModel] | None = Field(default=None, description="The blocks of the agent. This is used to specify the initial blocks of the agent")
    hash: str | None = Field(
        default=None,
        description="A unique hash of the agent definition. An agent can use this hash to check if its definition has changed and if it needs to update its implementations and states. This is used to optimize the update process by only updating the implementations and states that have changed.",
    )
    pass


@kante.pydantic_input(ImplementAgentInputModel, description="Implement an agent with the given implementations, states and locks. This will create the agent if it doesn't exist and update it if it does exist.")
class ImplementAgentInput:
    name: str | None = None
    locks: list[LockImplementationInput] | None = None
    states: list[StateImplementationInput] | None = None
    bloks: list[BlokImplementationInput] | None = None
    implementations: list[ImplementationInput] | None = None
    hash: str | None = None


@transaction.atomic
def implement_agent(info: Info, input: ImplementAgentInput) -> types.Agent:
    """Reconcile an agent's declared implementations/states/locks/bloks in one transaction.

    Atomicity matters here: a validation error on the Nth implementation (e.g. a malformed
    requires/provides descriptor key) must not leave the agent half-registered with the
    stale-implementation reap skipped — either the whole declared set lands, or none of it.
    """
    input = input.to_pydantic()

    # Before ANY row is read or written: a registration writes org-shared rows (actions,
    # protocols, collections, bloks) interleaved with agent-owned ones, so two agents of one
    # fleet registering at once would lock them in declaration order and deadlock. Taking it
    # before the Agent upsert also keeps that row's lock short — it is the row every heartbeat
    # renewal and every lease claim for this agent needs.
    lock_organization(info.context.request.organization)

    agent, _ = models.Agent.objects.update_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
        defaults=dict(
            name=input.name or f"{info.context.request.client.client_id}",
            app=info.context.request.client.release.app,
            release=info.context.request.client.release,
            hash=input.hash or str(uuid.uuid4()),
        ),
    )

    created_implementations_id = []
    created_implementations = []
    created_states_id = []
    created_states = []

    for lock in input.locks or []:
        # update_or_create: a redeclared description takes effect instead of being
        # silently kept from the first registration.
        models.Lock.objects.update_or_create(
            agent=agent,
            key=lock.key,
            defaults=dict(
                description=lock.definition.description,
            ),
        )

    # Batch prefetch for the per-implementation loop: one Action query + one Implementation
    # query for the whole declared set instead of two lookups per implementation. Scoped to
    # the agent's app/org, exactly what _create_implementation's per-row lookups filter on.
    # Deterministic order, so two registrations of overlapping sets behave identically.
    declared_implementations = sorted(input.implementations or [], key=lambda impl: (impl.definition.key, impl.definition.version))
    action_map = None
    implementation_map = None
    if declared_implementations:
        wanted = {(impl.definition.key, impl.definition.version) for impl in declared_implementations}
        action_map = {
            (action.key, action.version): action
            for action in models.Action.objects.filter(
                app=agent.app,
                organization=agent.organization,
                key__in={key for key, _ in wanted},
                version__in={version for _, version in wanted},
            )
        }
        implementation_map = {implementation.interface: implementation for implementation in models.Implementation.objects.filter(agent=agent).select_related("action")}

    for implementation in declared_implementations:
        created_implementation = _create_implementation(implementation, agent, action_map=action_map, implementation_map=implementation_map)

        created_implementations_id.append(created_implementation.id)
        created_implementations.append(created_implementation)

    for inputstate in input.states or []:
        state = _register_state(agent, inputstate)

        created_states_id.append(state.id)
        created_states.append(state)

    # Reap everything the agent no longer declares. Queryset delete still emits per-instance
    # signals (the subscription fan-out in facade.signals), without the per-row get() loops.
    #
    # Implementations carrying non-terminal tasks are kept: an agent that re-registers without an
    # implementation it is still executing must not have that work deleted out from under it.
    # ``Task.implementation`` is SET_NULL, so reaping an idle implementation preserves its history.
    models.State.objects.filter(agent=agent).exclude(id__in=created_states_id).delete()

    stale_implementations = models.Implementation.objects.filter(agent=agent).exclude(id__in=created_implementations_id)
    live = stale_implementations.filter(tasks__is_done=False).distinct()
    live_ids = list(live.values_list("id", flat=True))
    if live_ids:
        logger.warning(f"Keeping {len(live_ids)} undeclared implementation(s) for agent {agent.id}: still running tasks.")
    stale_implementations.exclude(id__in=live_ids).delete()

    for blok in input.bloks or []:
        catalog = models.UICatalog.objects.get_or_create(name=blok.catalog or "default", organization=agent.organization)[0]
        blok_diagnostics = validate_manifest_against_catalog(catalog, blok.components)

        x, _ = models.Blok.objects.update_or_create(
            name=blok.key,
            organization=agent.organization,
            defaults=dict(
                components=[x.model_dump() for x in blok.components] if blok.components else [],
                description=blok.description,
                creator=info.context.request.user,
                catalog=catalog,
                demo_state=blok.demo_state or {},
                diagnostics=dump_diagnostics(blok_diagnostics),
            ),
        )

        # One auto-materialization per agent-declared blok, every dependency bound to the
        # declaring agent itself.
        mblok, _ = models.MaterializedBlok.objects.update_or_create(
            blok=x,
            declared_by=agent,
            defaults=dict(name=x.name, description=x.description or ""),
        )

        for dep in _sync_blok_dependencies(x, blok.dependencies, replace=True):
            models.BlokAgentMapping.objects.update_or_create(
                materialized_blok=mblok,
                key=dep.key,
                defaults=dict(dependency=dep, agent=agent),
            )

    return agent


def pin_agent(info: Info, input: inputs.PinInput) -> types.Agent:
    agent = scoped_get(models.Agent, info, input.id)
    if input.pin:
        agent.pinned_by.add(info.context.request.user)
    else:
        agent.pinned_by.remove(info.context.request.user)
    # An M2M change needs no row write — a ``save()`` here only ever existed to fire the feed
    # refresh, at the price of rewriting the whole row from a possibly stale snapshot.
    signals.broadcast_agent_update(agent)
    return agent


def update_agent(info: Info, input: inputs.UpdateAgentInput) -> types.Agent:
    agent = scoped_get(models.Agent, info, input.id)
    if input.name is not None:
        agent.name = input.name
    agent.save(update_fields=["name"])
    return agent


def delete_agent(info: Info, input: DeleteAgentInput) -> strawberry.ID:
    agent = scoped_get(models.Agent, info, input.id)
    agent.delete()
    return input.id
