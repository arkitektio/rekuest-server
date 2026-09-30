from kante.types import Info
import strawberry
import datetime
from facade import types, models, scalars, logic
from typing import AsyncGenerator
from facade.channels import (
    state_update_channel,
    patch_channel,
)
from asgiref.sync import sync_to_async


async def state_update_events(
    self,
    info: Info,
    state_id: strawberry.ID,
) -> AsyncGenerator[types.State, None]:
    """Join and subscribe to message sent to the given rooms."""

    state = await models.State.objects.aget(id=state_id)

    async for message in state_update_channel.listen(info.context, [f"state_{state.id}"]):
        yield await models.State.objects.aget(id=message.state)


# Plain types for watch subscriptions (no model cross-references)


@strawberry.type(description="A plain snapshot of a state's current value.")
class StateSnapshotEvent:
    state_id: strawberry.ID
    agent_id: strawberry.ID
    interface: str
    value: scalars.Args
    global_revision: int
    session_id: str
    timestamp: datetime.datetime


@strawberry.type(description="A plain patch event with no model cross-references.")
class StatePatchEvent:
    state_id: strawberry.ID
    agent_id: strawberry.ID
    interface: str
    op: str
    path: str
    value: scalars.Args
    global_revision: int
    session_id: str
    timestamp: datetime.datetime


async def watch_state(
    self,
    info: Info,
    state_id: strawberry.ID | None = None,
    agent_id: strawberry.ID | None = None,
    interface: str | None = None,
) -> AsyncGenerator[StateSnapshotEvent | StatePatchEvent, None]:
    """Watch a state: yields the current snapshot then streams patches and state updates."""

    if state_id:
        state = await models.State.objects.select_related("agent").aget(id=state_id)
    else:
        state = await models.State.objects.select_related("agent").aget(
            agent_id=agent_id,
            interface=interface,
        )

    returned = await sync_to_async(logic.get_latest_state)(state.agent, state_id=state.id)

    yield StateSnapshotEvent(
        state_id=strawberry.ID(str(state_id)),
        agent_id=strawberry.ID(str(state.agent_id)),
        interface=state.interface,
        value=returned.get("states", {}).get(state.interface),
        global_revision=returned.get("global_revision", 0),
        session_id=returned.get("session_id"),
        timestamp=returned.get("timestamp"),
    )

    topics = [
        f"state_{state.id}",
        f"patches_state_{state.id}",
    ]

    async for message in patch_channel.listen(info.context, topics):
        # Payload-carrying: the PatchEvent brings the whole patch — no per-subscriber fetch.
        yield StatePatchEvent(
            state_id=strawberry.ID(str(message.state)),
            agent_id=strawberry.ID(str(message.agent)) if message.agent else strawberry.ID(""),
            op=message.op,
            path=message.path,
            value=message.value,
            global_revision=message.global_rev,
            session_id=message.session,
            timestamp=message.timestamp,
            interface=message.interface,
        )


@strawberry.type(description="A plain snapshot of a state's current value.")
class AgentSnapshotEvent:
    agent_id: strawberry.ID
    values: scalars.Args
    global_revision: int
    session_id: str
    timestamp: datetime.datetime


async def watch_agent(
    self,
    info: Info,
    agent_id: strawberry.ID,
) -> AsyncGenerator[AgentSnapshotEvent | StatePatchEvent, None]:
    """Watch an agent: yields current snapshots for all states then streams patches and state updates."""

    agent = await models.Agent.objects.aget(id=agent_id)

    # Yield a snapshot for each state of this agent
    state = await sync_to_async(logic.get_latest_state)(agent)

    topics = [f"patches_agent_{agent.pk}"]

    yield AgentSnapshotEvent(
        agent_id=strawberry.ID(str(agent.id)),
        values=state.get("states", {}),
        global_revision=state.get("global_revision", 0),
        session_id=state.get("session_id"),
        timestamp=state.get("timestamp"),
    )

    async for message in patch_channel.listen(info.context, topics):
        # Payload-carrying: no per-subscriber fetch; the topic already scopes to the agent.
        if not message.agent or str(message.agent) != str(agent.id):
            continue

        yield StatePatchEvent(
            state_id=strawberry.ID(str(message.state)),
            agent_id=strawberry.ID(str(message.agent)),
            op=message.op,
            path=message.path,
            value=message.value,
            global_revision=message.global_rev,
            session_id=message.session,
            timestamp=message.timestamp,
            interface=message.interface,
        )
