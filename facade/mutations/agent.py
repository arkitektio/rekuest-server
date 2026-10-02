import logging

import kante
import strawberry
from kante.types import Info
from pydantic import BaseModel, Field

from facade import takt, enums, inputs, models, signals, types
from facade.types.base import scoped_get
from rekuest_core.inputs.models import BlokImplementationInputModel, ImplementationInputModel, LockImplementationInputModel, StateImplementationInputModel
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
    payload: dict = {"principal": takt._principal(info), "clear_drawers": True}
    if input.name is not None:
        payload["name"] = input.name
    if input.description is not None:
        payload["description"] = input.description
    if input.kind is not None:
        payload["kind"] = getattr(input.kind, "value", input.kind)
    if input.hook_url is not None:
        payload["hook_url"] = input.hook_url
    if input.hook_url_secret is not None:
        payload["hook_url_secret"] = input.hook_url_secret
    answer = takt.call("agent/ensure", payload)
    return models.Agent.objects.get(pk=answer["agent"])


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
    """Reconcile the caller's agent's declared implementations/states/locks/bloks, atomically.

    Served by takt (``internal/agent/implement``): the same reconciliation a socket agent's
    REGISTER runs, so either the whole declared set lands or none of it.
    """
    model = input.to_pydantic()
    answer = takt.call("agent/implement", {"principal": takt._principal(info), "input": model.model_dump(mode="json", exclude_none=True)})
    return models.Agent.objects.get(pk=answer["agent"])


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
    """Rename an agent for its users. The declared name is the agent's own and stays; an empty name takes the rename back."""
    agent = scoped_get(models.Agent, info, input.id)
    if input.name is not None:
        agent.display_name = input.name.strip() or None
        agent.save(update_fields=["display_name"])
    return agent


def delete_agent(info: Info, input: DeleteAgentInput) -> strawberry.ID:
    """Delete an agent with everything below it. takt owns the rows: it kicks a connected agent and cascades."""
    agent = scoped_get(models.Agent, info, input.id)
    takt.call("agent/delete", {"principal": takt._principal(info), "agent": str(agent.pk)})
    return input.id
