import datetime

from kante.types import Info
import strawberry
from facade import channel_events, models, enums, types
from rekuest_core import scalars as rscalars
from typing import AsyncGenerator
from facade.channels import task_event_channel, child_task_channel, agent_task_channel


@strawberry.type(description="Slim, non-traversable snapshot of a task for change feeds.")
class TaskChange:
    id: strawberry.ID
    reference: str | None
    is_done: bool
    latest_event_kind: enums.TaskEventKind
    latest_instruct_kind: enums.TaskInstructKind
    status_message: str | None
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
            id=strawberry.ID(str(t.id)),
            reference=t.reference,
            is_done=t.is_done,
            latest_event_kind=enums.TaskEventKind(t.latest_event_kind),
            latest_instruct_kind=enums.TaskInstructKind(t.latest_instruct_kind),
            status_message=t.statusmessage or None,
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
            status_message=p.status_message,
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
    created_at: datetime.datetime

    @classmethod
    def from_model(cls, e: models.TaskEvent) -> "TaskEventChange":
        return cls(
            id=strawberry.ID(str(e.id)),
            task=strawberry.ID(str(e.task_id)),
            kind=enums.TaskEventKind(e.kind),
            message=e.message,
            progress=e.progress,
            returns=e.returns,
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
            returns=p.returns,
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


def _build_change(message) -> TaskChangeEvent:
    """Build a slim TaskChangeEvent from a payload-carrying channel message — no lookups."""
    if message.create:
        return TaskChangeEvent(create=TaskChange.from_payload(message.create), event=None)
    return TaskChangeEvent(event=TaskEventChange.from_payload(message.event), create=None)


async def mytasks(
    self,
    info: Info,
) -> AsyncGenerator[TaskChangeEvent, None]:
    """Subscribe to root tasks (and their events) created by this client (caller-scoped)."""

    caller, _ = await models.Caller.objects.aget_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
    )

    async for message in task_event_channel.listen(info.context, [f"root_tasks_caller_{caller.id}"]):
        yield _build_change(message)


async def tasks(
    self,
    info: Info,
) -> AsyncGenerator[TaskChangeEvent, None]:
    """Subscribe to root task changes (and their events) across the whole organization."""

    organization = info.context.request.organization

    async for message in task_event_channel.listen(info.context, [f"root_tasks_org_{organization.id}"]):
        yield _build_change(message)


@strawberry.type
class AgentTaskUpdate:
    create: TaskChange | None
    update: TaskChange | None


async def agent_tasks(
    self,
    info: Info,
    agent: strawberry.ID,
) -> AsyncGenerator[AgentTaskUpdate, None]:
    """Subscribe to task create/update for a single agent (its detail-page "latest tasks" feed)."""

    async for message in agent_task_channel.listen(info.context, [f"agent_tasks_{agent}"]):
        if message.create:
            yield AgentTaskUpdate(create=TaskChange.from_payload(message.create), update=None)
        elif message.update:
            yield AgentTaskUpdate(create=None, update=TaskChange.from_payload(message.update))


async def child_tasks(
    self,
    info: Info,
    id: strawberry.ID,
) -> AsyncGenerator[ChildTaskEvent, None]:
    """Subscribe to all descendant task changes of a given task (any task whose root or parent is it)."""

    task = await models.Task.objects.aget(id=id)

    async for message in child_task_channel.listen(info.context, [f"child_tasks_{task.id}"]):
        if message.create:
            yield ChildTaskEvent(create=TaskChange.from_payload(message.create), update=None)
        elif message.update:
            yield ChildTaskEvent(update=TaskChange.from_payload(message.update), create=None)
