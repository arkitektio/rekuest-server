from __future__ import annotations

import datetime
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from facade.json_types import JSON

if TYPE_CHECKING:
    from facade import models


class StateUpdateEvent(BaseModel):
    """A model representing a state update event."""

    state: int = Field(..., description="The state that was updated.")


class PatchEvent(BaseModel):
    """A payload-carrying patch event: one update of a state.

    Carries the full update so the watch subscriptions can relay it without re-fetching
    the row per subscriber (the same treatment the task feeds got). ``create``/``state``/
    ``agent`` keep their historic id shape for consumers that still fetch.
    """

    create: int = Field(..., description="The patch ID that was created.")
    state: int = Field(..., description="The state ID related to the patch.")
    agent: int | None = Field(
        default=None, description="The agent ID related to the patch."
    )
    interface: str = Field(
        default="", description="The interface of the state in the agent."
    )
    ops: list[dict[str, JSON]] = Field(
        default_factory=list,
        description="The update's RFC 6902 operations (op, path, value), to be applied in order and together.",
    )
    global_rev: int = Field(
        default=0, description="The global revision after this patch."
    )
    session: int | None = Field(
        default=None, description="The session ID related to the patch."
    )
    timestamp: datetime.datetime | None = Field(
        default=None, description="When the patch was created."
    )
    task: int | None = Field(
        default=None, description="The task that made the change, if one did."
    )
    lifecycle: str | None = Field(
        default=None,
        description="Where in a task's or the agent's life the change was made, when not in its ordinary course.",
    )
    qualifiers: list[str] = Field(
        default_factory=list, description="What the app says the change was for."
    )

    @classmethod
    def from_patch(cls, p: models.Patch) -> PatchEvent:
        return cls(
            create=p.pk,
            state=p.state_id,
            agent=p.agent_id,
            interface=p.interface,
            ops=list(p.ops),
            global_rev=p.global_rev,
            session=p.session_id,
            timestamp=p.timestamp,
            task=p.task_id,
            lifecycle=p.lifecycle,
            qualifiers=list(p.qualifiers or []),
        )


def _kind_value(kind: Enum | str) -> str:
    """Normalize a kind that may be a TextChoices/str-enum member (whose ``str()`` is
    ``"Cls.NAME"``) or a plain string to the raw choices value."""
    return str(kind.value) if isinstance(kind, Enum) else kind


class TaskChangePayload(BaseModel):
    """A payload-carrying snapshot of a task for the change feeds.

    Producers (the post_save signals) already hold the row, so carrying its fields costs
    nothing — while relaying only the PK forced every subscriber to re-SELECT the row per
    message. Field set mirrors the slim ``TaskChange`` GraphQL type.
    """

    id: str
    reference: str | None = None
    is_done: bool = False
    latest_event_kind: str
    latest_instruct_kind: str
    action: str
    implementation: str | None = None
    agent: str | None = None
    root: str | None = None
    parent: str | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    finished_at: datetime.datetime | None = None
    # Monotonic per-task version. Several backends write the same task (an agent report here, a
    # cancel or a sweep there) and the channel layer does not deliver in commit order — a
    # consumer applies a change only if its revision is greater than the last one it applied.
    revision: int = 0

    @classmethod
    def from_task(cls, t: models.Task) -> TaskChangePayload:
        return cls(
            id=str(t.pk),
            # ``Task.reference`` defaults to the ``uuid.uuid4`` callable, so an in-memory
            # instance can carry a UUID object rather than its persisted string form.
            reference=str(t.reference) if t.reference is not None else None,
            is_done=t.is_done,
            latest_event_kind=_kind_value(t.latest_event_kind),
            latest_instruct_kind=_kind_value(t.latest_instruct_kind),
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
    message: str | None = None
    progress: int | None = None
    returns: JSON = None
    level: str | None = None
    # EFFECT: the value taken; LOST: what is known (started, last progress, effects).
    value: JSON = None
    created_at: datetime.datetime
    # The event's number within its task, in commit order (takt's ``insert_event``): the
    # cursor a caller session replays from. None from a takt older than 0028.
    task_seq: int | None = None

    @classmethod
    def from_event(cls, e: models.TaskEvent) -> TaskEventPayload:
        return cls(
            id=str(e.pk),
            task=str(e.task_id),
            kind=_kind_value(e.kind),
            message=e.message,
            progress=e.progress,
            returns=e.returns,
            level=_kind_value(e.level) if e.level else None,
            value=e.value,
            created_at=e.created_at,
            task_seq=e.task_seq,
        )


class TaskEventCreatedEvent(BaseModel):
    """A task feed message: either a persisted event or a freshly created root task."""

    event: TaskEventPayload | None = Field(
        default=None, description="The event that was created."
    )
    create: TaskChangePayload | None = Field(
        default=None, description="The task created."
    )


class ChildTaskEvent(BaseModel):
    """A model representing a child task event."""

    create: TaskChangePayload | None = Field(
        default=None, description="The task that was created."
    )
    update: TaskChangePayload | None = Field(
        default=None, description="The task that was updated."
    )


class ProbeEventBroadcast(BaseModel):
    """A payload-carrying event of a probe.

    Probes have no DB rows, so unlike the Task feeds this carries the full event — the
    subscription layer relays it as-is with zero lookups. ``seq`` is the per-probe redis
    counter (monotonic across processes): the ordering and dedup key of the probe stream.
    """

    probe: str = Field(..., description="The probe id (p-…).")
    kind: str = Field(..., description="The TaskEventKind value of this event.")
    seq: int = Field(..., description="Per-probe monotonic sequence number.")
    message: str | None = Field(
        default=None, description="Optional human-readable message / error."
    )
    level: str | None = Field(default=None, description="Log level for LOG events.")
    progress: int | None = Field(
        default=None, description="Progress (0-100) for PROGRESS events."
    )
    returns: JSON = Field(default=None, description="Returns payload for YIELD events.")
    created_at: datetime.datetime = Field(
        ..., description="Server-side time the event was recorded."
    )


class AgentEvent(BaseModel):
    """A model representing an agent event."""

    create: int | None = Field(default=None, description="The agent that was created.")
    update: int | None = Field(default=None, description="The agent that was updated.")
    delete: int | None = Field(default=None, description="The agent that was deleted.")


class AssetEvent(BaseModel):
    """An asset was stored: its id, the reference bloks name it by, and what it is."""

    create: int = Field(..., description="The asset that was stored.")
    ref: str = Field(..., description="'sha256:' and the hex SHA-256 of its bytes.")
    kind: str = Field(..., description="What the file is (rekuest_core AssetKind).")


class ImplementationEvent(BaseModel):
    """A model representing a template event."""

    create: int | None = Field(
        default=None, description="The template that was created."
    )
    update: int | None = Field(
        default=None, description="The template that was updated."
    )
    delete: int | None = Field(
        default=None, description="The template that was deleted."
    )


class SignalFeedEvent(BaseModel):
    """A signal arrived (``create``) or was matched against the triggers (``update``); ids only."""

    create: int | None = Field(default=None, description="The signal that arrived.")
    update: int | None = Field(
        default=None, description="The signal that was processed."
    )


class RuleFeedEvent(BaseModel):
    """A schedule or trigger was created, changed or deleted; ids only."""

    schedule: int | None = Field(default=None, description="The schedule concerned.")
    trigger: int | None = Field(default=None, description="The trigger concerned.")
    change: str = Field(..., description="create, update or delete")
