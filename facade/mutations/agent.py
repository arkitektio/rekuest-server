import logging
from typing import cast

import kante
import strawberry
from kante.types import Info

from facade import enums, inputs, models, signals, takt, takt_api, types
from facade.caller_context import CallerContext
from facade.takt_api import Principal
from facade.types.base import scoped_get
from rekuest_core.inputs.types import BlokImplementationInput, ImplementationInput, LockImplementationInput, StateImplementationInput

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
    """Create (or find) the caller's agent, configure its transport, forget what it had shelved.

    Served by takt (``internal/agent/ensure``), which owns agent rows. For dashboards and a
    HookAgent's bootstrap (``kind``, ``hook_url``, ``hook_url_secret``), which has no socket to
    register over; an agent turning WEBHOOK has its socket queue abandoned there.
    """
    # Only what the input gives is sent: to takt a field that is there and null means "clear".
    request = takt_api.EnsureAgentRequest(principal=Principal.of(CallerContext.from_info(info)), clear_drawers=True)
    if input.name is not None:
        request.name = input.name
    if input.description is not None:
        request.description = input.description
    if input.kind is not None:
        request.kind = input.kind.value
    if input.hook_url is not None:
        request.hook_url = input.hook_url
    if input.hook_url_secret is not None:
        request.hook_url_secret = input.hook_url_secret
    return cast("types.Agent", models.Agent.objects.get(pk=takt.call(takt_api.ENSURE_AGENT, request).agent))


@kante.pydantic_input(inputs.ImplementAgentInputModel, description="Implement an agent with the given implementations, states and locks. This will create the agent if it doesn't exist and update it if it does exist.")
class ImplementAgentInput:
    name: str | None = None
    description: str | None = None
    locks: list[LockImplementationInput] | None = None
    states: list[StateImplementationInput] | None = None
    bloks: list[BlokImplementationInput] | None = None
    implementations: list[ImplementationInput] | None = None
    hash: str | None = None


def implement_agent(info: Info, input: ImplementAgentInput) -> types.Agent:
    """Reconcile the caller's agent's declared implementations/states/locks/bloks, atomically.

    Served by takt (``internal/agent/implement``): the same reconciliation a socket agent's
    REGISTER runs, so either the whole declared set lands or none of it.
    """
    request = takt_api.ImplementAgentRequest(principal=Principal.of(CallerContext.from_info(info)), input=input.to_pydantic())
    return cast("types.Agent", models.Agent.objects.get(pk=takt.call(takt_api.IMPLEMENT_AGENT, request).agent))


def pin_agent(info: Info, input: inputs.PinInput) -> types.Agent:
    data = input.to_pydantic()
    agent = scoped_get(models.Agent, info, data.id)
    if data.pin:
        agent.pinned_by.add(CallerContext.from_info(info).user)
    else:
        agent.pinned_by.remove(CallerContext.from_info(info).user)
    # An M2M change needs no row write — a ``save()`` here only ever existed to fire the feed
    # refresh, at the price of rewriting the whole row from a possibly stale snapshot.
    signals.broadcast_agent_update(agent)
    return cast("types.Agent", agent)


def update_agent(info: Info, input: inputs.UpdateAgentInput) -> types.Agent:
    """Rename an agent for its users. The declared name is the agent's own and stays; an empty name takes the rename back."""
    data = input.to_pydantic()
    agent = scoped_get(models.Agent, info, data.id)
    if data.name is not None:
        agent.display_name = data.name.strip() or None
        agent.save(update_fields=["display_name"])
    return cast("types.Agent", agent)


def delete_agent(info: Info, input: DeleteAgentInput) -> strawberry.ID:
    """Delete an agent with everything below it. takt owns the rows: it kicks a connected agent and cascades."""
    agent = scoped_get(models.Agent, info, input.id)
    takt.call(takt_api.DELETE_AGENT, takt_api.AgentRequest(principal=Principal.of(CallerContext.from_info(info)), agent=str(agent.pk)))
    return input.id
