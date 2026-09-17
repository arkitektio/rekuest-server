import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class DBEvent(BaseModel):
    """A model representing a database event."""

    event_type: str = Field(..., description="Type of the event (e.g., 'insert', 'update', 'delete').")


class StateUpdateEvent(BaseModel):
    """A model representing a state update event."""

    state: int = Field(..., description="The state that was updated.")


class PatchEvent(BaseModel):
    """A payload-carrying patch event.

    Carries the full patch so the watch subscriptions can relay it without re-fetching
    the row per subscriber (the same treatment the task feeds got). ``create``/``state``/
    ``agent`` keep their historic id shape for consumers that still fetch.
    """

    create: int = Field(..., description="The patch ID that was created.")
    state: int = Field(..., description="The state ID related to the patch.")
    agent: int | None = Field(None, description="The agent ID related to the patch.")
    interface: str = Field("", description="The interface of the state in the agent.")
    op: str = Field("", description="The patch operation (add, remove, replace, …).")
    path: str = Field("", description="The path the patch applies to.")
    value: Optional[Any] = Field(None, description="The patch value.")
    global_rev: int = Field(0, description="The global revision after this patch.")
    session: Optional[int] = Field(None, description="The session ID related to the patch.")
    timestamp: Optional[datetime.datetime] = Field(None, description="When the patch was created.")

    @classmethod
    def from_patch(cls, p) -> "PatchEvent":
        return cls(
            create=p.id,
            state=p.state_id,
            agent=p.agent_id,
            interface=p.interface,
            op=p.op,
            path=p.path,
            value=p.value,
            global_rev=p.global_rev,
            session=p.session_id,
            timestamp=p.timestamp,
        )


def _kind_value(kind: Any) -> str:
    """Normalize a kind that may be a TextChoices/str-enum member (whose ``str()`` is
    ``"Cls.NAME"``) or a plain string to the raw choices value."""
    return kind.value if hasattr(kind, "value") else str(kind)


class TaskChangePayload(BaseModel):
    """A payload-carrying snapshot of a task for the change feeds.

    Producers (the post_save signals) already hold the row, so carrying its fields costs
    nothing — while relaying only the PK forced every subscriber to re-SELECT the row per
    message. Field set mirrors the slim ``TaskChange`` GraphQL type.
    """

    id: str
    reference: Optional[str] = None
    is_done: bool = False
    latest_event_kind: str
    latest_instruct_kind: str
    status_message: Optional[str] = None
    action: str
    implementation: Optional[str] = None
    agent: Optional[str] = None
    root: Optional[str] = None
    parent: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    finished_at: Optional[datetime.datetime] = None
    # Monotonic per-task version. Several backends write the same task (an agent report here, a
    # cancel or a sweep there) and the channel layer does not deliver in commit order — a
    # consumer applies a change only if its revision is greater than the last one it applied.
    revision: int = 0

    @classmethod
    def from_task(cls, t) -> "TaskChangePayload":
        return cls(
            id=str(t.id),
            # ``Task.reference`` defaults to the ``uuid.uuid4`` callable, so an in-memory
            # instance can carry a UUID object rather than its persisted string form.
            reference=str(t.reference) if t.reference is not None else None,
            is_done=t.is_done,
            latest_event_kind=_kind_value(t.latest_event_kind),
            latest_instruct_kind=_kind_value(t.latest_instruct_kind),
            status_message=t.statusmessage or None,
            action=str(t.action_id),
            implementation=str(t.implementation_id) if t.implementation_id else None,
            agent=str(t.agent_id) if t.agent_id else None,
            root=str(t.root_id) if t.root_id else None,
            parent=str(t.parent_id) if t.parent_id else None,
            created_at=t.created_at,
            updated_at=t.updated_at,
            finished_at=t.finished_at,
            revision=t.revision if isinstance(t.revision, int) else 0,
        )


class TaskEventPayload(BaseModel):
    """A payload-carrying persisted task event for the change feeds.

    ``id`` stays the TaskEvent PK: consumers keep deriving the caller protocol's ``event``
    dedup handle and monotonic ``seq`` from it, exactly as before — just without the
    per-subscriber re-SELECT.
    """

    id: str
    task: str
    kind: str
    message: Optional[str] = None
    progress: Optional[int] = None
    returns: Optional[Any] = None
    level: Optional[str] = None
    created_at: datetime.datetime

    @classmethod
    def from_event(cls, e) -> "TaskEventPayload":
        return cls(
            id=str(e.id),
            task=str(e.task_id),
            kind=_kind_value(e.kind),
            message=e.message,
            progress=e.progress,
            returns=e.returns,
            level=_kind_value(e.level) if e.level else None,
            created_at=e.created_at,
        )


class TaskEventCreatedEvent(BaseModel):
    """A task feed message: either a persisted event or a freshly created root task."""

    event: TaskEventPayload | None = Field(None, description="The event that was created.")
    create: TaskChangePayload | None = Field(None, description="The task created.")


class ChildTaskEvent(BaseModel):
    """A model representing a child task event."""

    create: TaskChangePayload | None = Field(None, description="The task that was created.")
    update: TaskChangePayload | None = Field(None, description="The task that was updated.")


class ProbeEventBroadcast(BaseModel):
    """A payload-carrying event of a probe.

    Probes have no DB rows, so unlike the Task feeds this carries the full event — the
    subscription layer relays it as-is with zero lookups. ``seq`` is the per-probe redis
    counter (monotonic across processes): the ordering and dedup key of the probe stream.
    """

    probe: str = Field(..., description="The probe id (p-…).")
    kind: str = Field(..., description="The TaskEventKind value of this event.")
    seq: int = Field(..., description="Per-probe monotonic sequence number.")
    message: Optional[str] = Field(None, description="Optional human-readable message / error.")
    level: Optional[str] = Field(None, description="Log level for LOG events.")
    progress: Optional[int] = Field(None, description="Progress (0-100) for PROGRESS events.")
    returns: Optional[Any] = Field(None, description="Returns payload for YIELD events.")
    created_at: datetime.datetime = Field(..., description="Server-side time the event was recorded.")


class AgentEvent(BaseModel):
    """A model representing an agent event."""

    create: int | None = Field(None, description="The agent that was created.")
    update: int | None = Field(None, description="The agent that was updated.")
    delete: int | None = Field(None, description="The agent that was deleted.")


class ImplementationEvent(BaseModel):
    """A model representing a template event."""

    create: int | None = Field(None, description="The template that was created.")
    update: int | None = Field(None, description="The template that was updated.")
    delete: int | None = Field(None, description="The template that was deleted.")


class ActionEvent(BaseModel):
    """A model representing an action event."""

    create: int | None = Field(None, description="The action that was created.")
    update: int | None = Field(None, description="The action that was updated.")
    delete: int | None = Field(None, description="The action that was deleted.")
