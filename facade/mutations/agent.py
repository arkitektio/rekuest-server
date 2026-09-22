from django.db import transaction
from kante.types import Info
import strawberry
from facade import types, models, inputs, enums, signals
from rekuest_core.inputs.types import BlokImplementationInput, ImplementationInput, LockImplementationInput, StateImplementationInput
from rekuest_core.inputs.models import BlokImplementationInputModel, ImplementationInputModel, StateImplementationInputModel, LockImplementationInputModel
import logging
from facade import registration
from pydantic import BaseModel, Field
import kante
from facade.types.base import scoped_get

logger = logging.getLogger(__name__)


@strawberry.input
class AgentInput:
    name: str | None = strawberry.field(
        default=None,
        description="The name of the agent. This is used to identify the agent in the system.",
    )
    description: str | None = strawberry.field(
        default=None,
        description="What this agent is, in a sentence. Omitting it leaves whatever the agent already has: a name is what identifies it, a description is what tells two of them apart.",
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


def ensure_agent(info: Info, input: AgentInput) -> types.Agent:
    """Create (or find) the caller's agent and forget what a previous process shelved.

    The socket ``Register`` does the same through :func:`facade.registration.ensure_agent`;
    this mutation remains for dashboards and for a HookAgent's bootstrap (``kind``,
    ``hook_url``, ``hook_url_secret``), which cannot register over a socket it does not have.
    """
    request = info.context.request
    agent = registration.ensure_agent(request.client, request.user, request.organization, name=input.name)
    registration.clear_drawers(agent)

    # Configure the transport (idempotent): a HookAgent declares its kind + endpoint here.
    updated_fields = []
    became_webhook = False
    if input.description is not None:
        agent.description = input.description
        updated_fields.append("description")
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
    description: str | None = Field(default=None, description="What this agent is, in a sentence. Omitting it leaves whatever the agent already has, unlike `name`, which falls back to the client id.")
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
    description: str | None = None
    locks: list[LockImplementationInput] | None = None
    states: list[StateImplementationInput] | None = None
    bloks: list[BlokImplementationInput] | None = None
    implementations: list[ImplementationInput] | None = None
    hash: str | None = None


def implement_agent(info: Info, input: ImplementAgentInput) -> types.Agent:
    """Reconcile an agent's declared implementations/states/locks/bloks in one transaction.

    The body is :func:`facade.registration.implement_agent`, shared with the socket
    ``Implement`` message; it is atomic, so either the whole declared set lands or none of it.
    """
    request = info.context.request
    agent, _ = registration.implement_agent(request.client, request.user, request.organization, input.to_pydantic())
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
