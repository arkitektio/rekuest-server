import datetime
from typing import AsyncGenerator

import strawberry
from asgiref.sync import sync_to_async
from kante.types import Info

from facade import channel_events, enums, models
from facade.channels import agent_task_channel, child_task_channel, implementation_task_events, task_event_channel
from facade.types.base import scoped_get
from rekuest_core import scalars as rscalars


@strawberry.type(description="Slim, non-traversable snapshot of a task for change feeds.")
class TaskChange:
    id: strawberry.ID
    reference: str | None
    is_done: bool
    latest_event_kind: enums.TaskEventKind
    latest_instruct_kind: enums.TaskInstructKind
    action: strawberry.ID
    implementation: strawberry.ID | None
    agent: strawberry.ID | None
    root: strawberry.ID | None
    parent: strawberry.ID | None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    finished_at: datetime.datetime | None
    revision: int = strawberry.field(description="Monotonic per-task version. Changes are produced by several backends and may arrive out of order: apply one only if its revision is greater than the last you applied.")

    @classmethod
    def from_model(cls, t: models.Task) -> "TaskChange":
        return cls(
            id=strawberry.ID(str(t.pk)),
            reference=t.reference,
            is_done=t.is_done,
            latest_event_kind=enums.TaskEventKind(t.latest_event_kind),
            latest_instruct_kind=enums.TaskInstructKind(t.latest_instruct_kind),
            action=strawberry.ID(str(t.action_id)),
            implementation=strawberry.ID(str(t.implementation_id)) if t.implementation_id else None,
            agent=strawberry.ID(str(t.agent_id)) if t.agent_id else None,
            root=strawberry.ID(str(t.root_id)) if t.root_id else None,
            parent=strawberry.ID(str(t.parent_id)) if t.parent_id else None,
            created_at=t.created_at,
            updated_at=t.updated_at,
            finished_at=t.finished_at,
            revision=t.revision if isinstance(t.revision, int) else 0,
        )

    @classmethod
    def from_payload(cls, p: channel_events.TaskChangePayload) -> "TaskChange":
        return cls(
            id=strawberry.ID(p.id),
            reference=p.reference,
            is_done=p.is_done,
            latest_event_kind=enums.TaskEventKind(p.latest_event_kind),
            latest_instruct_kind=enums.TaskInstructKind(p.latest_instruct_kind),
            action=strawberry.ID(p.action),
            implementation=strawberry.ID(p.implementation) if p.implementation else None,
            agent=strawberry.ID(p.agent) if p.agent else None,
            root=strawberry.ID(p.root) if p.root else None,
            parent=strawberry.ID(p.parent) if p.parent else None,
            created_at=p.created_at,
            updated_at=p.updated_at,
            finished_at=p.finished_at,
            revision=p.revision,
        )


@strawberry.type(description="Slim, non-traversable task event for change feeds.")
class TaskEventChange:
    id: strawberry.ID
    task: strawberry.ID
    kind: enums.TaskEventKind
    message: str | None
    progress: int | None
    returns: rscalars.AnyDefault | None
    value: rscalars.AnyDefault | None = strawberry.field(default=None, description="EFFECT: the value taken. LOST: what is known (started, last_progress, effects, reason).")
    created_at: datetime.datetime

    @classmethod
    def from_model(cls, e: models.TaskEvent) -> "TaskEventChange":
        return cls(
            id=strawberry.ID(str(e.pk)),
            task=strawberry.ID(str(e.task_id)),
            kind=enums.TaskEventKind(e.kind),
            message=e.message,
            progress=e.progress,
            returns=e.returns,
            value=e.value,
            created_at=e.created_at,
        )

    @classmethod
    def from_payload(cls, p: channel_events.TaskEventPayload) -> "TaskEventChange":
        return cls(
            id=strawberry.ID(p.id),
            task=strawberry.ID(p.task),
            kind=enums.TaskEventKind(p.kind),
            message=p.message,
            progress=p.progress,
            returns=rscalars.AnyDefault(p.returns) if p.returns is not None else None,
            value=rscalars.AnyDefault(p.value) if p.value is not None else None,
            created_at=p.created_at,
        )


@strawberry.type
class TaskChangeEvent:
    event: TaskEventChange | None
    create: TaskChange | None


@strawberry.type
class ChildTaskEvent:
    create: TaskChange | None
    update: TaskChange | None


def _build_change(message: channel_events.TaskEventCreatedEvent) -> TaskChangeEvent:
    """Build a slim TaskChangeEvent from a payload-carrying channel message — no lookups."""
    if message.create:
        return TaskChangeEvent(create=TaskChange.from_payload(message.create), event=None)
    if message.event is None:
        raise ValueError("A task feed message carries neither a new task nor an event")
    return TaskChangeEvent(event=TaskEventChange.from_payload(message.event), create=None)


async def mytasks(
    self: object,
    info: Info,
) -> AsyncGenerator[TaskChangeEvent, None]:
    """Subscribe to root tasks (and their events) created by this client (caller-scoped)."""

    caller, _ = await models.Caller.objects.aget_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
    )

    async for message in task_event_channel.listen(info, [f"root_tasks_caller_{caller.pk}"]):
        yield _build_change(message)


async def tasks(
    self: object,
    info: Info,
) -> AsyncGenerator[TaskChangeEvent, None]:
    """Subscribe to root task changes (and their events) across the whole organization."""

    organization = info.context.request.organization

    async for message in task_event_channel.listen(info, [f"root_tasks_org_{organization.id}"]):
        yield _build_change(message)


@strawberry.type
class AgentTaskUpdate:
    create: TaskChange | None
    update: TaskChange | None


async def agent_tasks(
    self: object,
    info: Info,
    agent: strawberry.ID,
) -> AsyncGenerator[AgentTaskUpdate, None]:
    """Subscribe to task create/update for a single agent (its detail-page "latest tasks" feed)."""

    async for message in agent_task_channel.listen(info, [f"agent_tasks_{agent}"]):
        if message.create:
            yield AgentTaskUpdate(create=TaskChange.from_payload(message.create), update=None)
        elif message.update:
            yield AgentTaskUpdate(create=None, update=TaskChange.from_payload(message.update))


async def child_tasks(
    self: object,
    info: Info,
    id: strawberry.ID,
) -> AsyncGenerator[ChildTaskEvent, None]:
    """Subscribe to all descendant task changes of a given task (any task whose root or parent is it)."""

    task = await models.Task.objects.aget(id=id)

    async for message in child_task_channel.listen(info, [f"child_tasks_{task.pk}"]):
        if message.create:
            yield ChildTaskEvent(create=TaskChange.from_payload(message.create), update=None)
        elif message.update:
            yield ChildTaskEvent(update=TaskChange.from_payload(message.update), create=None)


TERMINAL_KINDS = ("COMPLETED", "CANCELLED", "INTERRUPTED", "FAILED", "CRITICAL", "LOST", "DENIED")
"""The kinds that end a task (takt's ``TERMINAL_KINDS``)."""

REPORTED_KINDS = ("PROGRESS", "YIELD", *TERMINAL_KINDS)
"""What a task's record is made of: how far it is, what it returned, how it ended."""


@strawberry.type(
    description="The task an implementation last ran as, with what its record is made of. The first message of `latestTask`, and again whenever a newer task takes its place."
)
class LatestTaskSnapshot:
    implementation: strawberry.ID | None = strawberry.field(description="The implementation the agent offers under the interface; null if it offers none, and nothing follows then.")
    latest: TaskChange | None = strawberry.field(description="The task it last ran as, whoever started it and whether or not it has ended; null if it never ran. (Not `task`: beside `TaskEventChange.task`, an id, one selection could not ask for both.)")
    events: list[TaskEventChange] = strawberry.field(description="The task's last PROGRESS, last YIELD and its terminal event, oldest first: enough to say how far it is, what it returned and how it ended.")


def read_latest_task(implementation: int) -> tuple[LatestTaskSnapshot, int]:
    """The snapshot of an implementation's latest task, and the newest event id it accounts for.

    Two queries: the task (one probe of ``task_impl_created_idx``), and the newest event of
    each kind its record is made of.
    """
    reference = strawberry.ID(str(implementation))
    task = models.Task.objects.filter(implementation_id=implementation).order_by("-created_at", "-id").first()
    if task is None:
        return LatestTaskSnapshot(implementation=reference, latest=None, events=[]), 0
    events = sorted(
        models.TaskEvent.objects.filter(task=task, kind__in=REPORTED_KINDS).order_by("kind", "-id").distinct("kind"),
        key=lambda event: event.pk,
    )
    return (
        LatestTaskSnapshot(implementation=reference, latest=TaskChange.from_model(task), events=[TaskEventChange.from_model(event) for event in events]),
        max((event.pk for event in events), default=0),
    )


def relay_latest_task(message: channel_events.TaskEventCreatedEvent, *, task: int, seen: int) -> TaskChange | TaskEventChange | None:
    """What a message of the implementation's feed is to someone holding a snapshot, or None.

    ``task`` is the snapshot's task and ``seen`` its newest event (0 for none): a message
    published before the snapshot was read says nothing the snapshot did not.
    """
    if message.create is not None:
        return None if int(message.create.id) < task else TaskChange.from_payload(message.create)
    if message.event is None or int(message.event.task) < task or int(message.event.id) <= seen:
        return None
    return TaskEventChange.from_payload(message.event)


async def latest_task(
    self: object,
    info: Info,
    agent: strawberry.ID,
    interface: str,
) -> AsyncGenerator[LatestTaskSnapshot | TaskChange | TaskEventChange, None]:
    """The task an agent's action last ran as, kept current: a snapshot, then what changes it."""
    found = await sync_to_async(scoped_get)(models.Agent, info, agent)
    implementation = await models.Implementation.objects.filter(agent=found, interface=interface).afirst()
    if implementation is None:
        yield LatestTaskSnapshot(implementation=None, latest=None, events=[])
        return

    # Joined before the snapshot is read, unlike ``task_event_channel.listen`` would: what is
    # published while the snapshot is read waits in the queue instead of being missed.
    async with info.context.consumer.listen_to_channel(  # type: ignore[union-attr]
        task_event_channel.message_type, groups=[implementation_task_events(implementation.pk)]
    ) as messages:
        snapshot, seen = await sync_to_async(read_latest_task)(implementation.pk)
        yield snapshot
        task = int(snapshot.latest.id) if snapshot.latest is not None else 0

        async for raw in messages:
            # Payload-carrying and already scoped by its group: no lookup per message.
            relayed = relay_latest_task(task_event_channel.model.model_validate(raw.get("message")), task=task, seen=seen)
            if relayed is not None:
                yield relayed

