import datetime
from collections.abc import AsyncGenerator
from typing import cast

import strawberry
from asgiref.sync import sync_to_async
from kante.types import Info

from facade import logic, models, scalars, types
from facade.channels import (
    patch_channel,
    state_update_channel,
)
from facade.types.state import JSONPatch


async def state_update_events(
    self: object,
    info: Info,
    state_id: strawberry.ID,
) -> AsyncGenerator[types.State, None]:
    """Join and subscribe to message sent to the given rooms."""

    state = await models.State.objects.aget(id=state_id)

    async for message in state_update_channel.listen(info, [f"state_{state.pk}"]):
        yield cast("types.State", await models.State.objects.aget(id=message.state))


# Plain types for watch subscriptions (no model cross-references)


@strawberry.type(description="A plain snapshot of a state's current value.")
class StateSnapshotEvent:
    state_id: strawberry.ID
    agent_id: strawberry.ID
    interface: str
    value: scalars.Args
    global_revision: int
    session_id: str | None
    timestamp: datetime.datetime | None


@strawberry.type(
    description="One update of a state, with no model cross-references. Its operations are applied in order and together: the state is at `globalRevision` once all of them are."
)
class StatePatchEvent:
    state_id: strawberry.ID
    agent_id: strawberry.ID
    interface: str
    ops: list[JSONPatch]
    global_revision: int
    session_id: str | None
    timestamp: datetime.datetime | None
    task_id: strawberry.ID | None = strawberry.field(
        default=None, description="The task that made the change, if one did."
    )
    lifecycle: str | None = strawberry.field(
        default=None,
        description="Where in a task's or the agent's life the change was made, when not in its ordinary course: `cleanup` or `shutdown`.",
    )
    qualifiers: list[str] = strawberry.field(
        default_factory=list,
        description="What the app says the change was for: its own names.",
    )


async def watch_state(
    self: object,
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

    returned = await sync_to_async(logic.get_latest_state)(
        state.agent, state_id=state.pk
    )

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
        f"state_{state.pk}",
        f"patches_state_{state.pk}",
    ]

    async for message in patch_channel.listen(info, topics):
        # Payload-carrying: the PatchEvent brings the whole patch — no per-subscriber fetch.
        yield StatePatchEvent(
            state_id=strawberry.ID(str(message.state)),
            agent_id=strawberry.ID(str(message.agent))
            if message.agent
            else strawberry.ID(""),
            ops=JSONPatch.of(message.ops),
            global_revision=message.global_rev,
            session_id=str(message.session) if message.session is not None else None,
            timestamp=message.timestamp,
            interface=message.interface,
            task_id=strawberry.ID(str(message.task))
            if message.task is not None
            else None,
            lifecycle=message.lifecycle,
            qualifiers=list(message.qualifiers),
        )


@strawberry.type(description="A plain snapshot of a state's current value.")
class AgentSnapshotEvent:
    agent_id: strawberry.ID
    values: scalars.Args
    global_revision: int
    session_id: str | None
    timestamp: datetime.datetime | None


async def watch_agent(
    self: object,
    info: Info,
    agent_id: strawberry.ID,
) -> AsyncGenerator[AgentSnapshotEvent | StatePatchEvent, None]:
    """Watch an agent: yields current snapshots for all states then streams patches and state updates."""

    agent = await models.Agent.objects.aget(id=agent_id)

    # Yield a snapshot for each state of this agent
    state = await sync_to_async(logic.get_latest_state)(agent)

    topics = [f"patches_agent_{agent.pk}"]

    yield AgentSnapshotEvent(
        agent_id=strawberry.ID(str(agent.pk)),
        values=state.get("states", {}),
        global_revision=state.get("global_revision", 0),
        session_id=state.get("session_id"),
        timestamp=state.get("timestamp"),
    )

    async for message in patch_channel.listen(info, topics):
        # Payload-carrying: no per-subscriber fetch; the topic already scopes to the agent.
        if not message.agent or str(message.agent) != str(agent.pk):
            continue

        yield StatePatchEvent(
            state_id=strawberry.ID(str(message.state)),
            agent_id=strawberry.ID(str(message.agent)),
            ops=JSONPatch.of(message.ops),
            global_revision=message.global_rev,
            session_id=str(message.session) if message.session is not None else None,
            timestamp=message.timestamp,
            interface=message.interface,
            task_id=strawberry.ID(str(message.task))
            if message.task is not None
            else None,
            lifecycle=message.lifecycle,
            qualifiers=list(message.qualifiers),
        )
