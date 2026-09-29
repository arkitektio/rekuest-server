"""Map a persisted ``TaskEvent`` to its caller-bound socket message.

The calling participant (the one that originated work via ``AssignRequest``) receives a
minimal per-kind ``…Event`` mirror of each task event over its own socket — no
GraphQL. This module is the single, pure, DB-free place that decides which ``…Event``
class an event kind maps to and how its fields are populated, so it is trivially
unit-testable with a light event-like stub.

``seq`` and ``event`` both derive from the event's primary key on purpose: ``event`` is a
stable dedup handle, ``seq`` the monotonic ordering / gap-detection key.
"""

from __future__ import annotations

from typing import Optional, Protocol, cast

from facade import messages
from facade.enums import TaskEventChoices as Kind

_LOG_LEVELS = {"DEBUG", "INFO", "ERROR", "WARN", "CRITICAL"}


class EventLike(Protocol):
    """The minimal surface ``build_execution_event`` reads — satisfied by the ORM model and test stubs."""

    id: int
    task_id: int
    kind: object  # a TextChoices/str-enum member or a plain string; normalized in build_execution_event
    message: Optional[str]
    progress: Optional[int]
    returns: Optional[dict]
    level: Optional[str]
    value: Optional[object]


def _base_kwargs(event: EventLike) -> dict:
    return {
        "task": str(event.task_id),
        "event": str(event.id),
        "seq": int(event.id),
    }


def build_execution_event(event: EventLike) -> Optional[messages.ExecutionEventMessage]:
    """Build the ``…Event`` message mirroring ``event``, or ``None`` if its kind is not forwarded.

    Keys off the persisted ``kind`` string (``TaskEventChoices`` values). Unknown /
    not-forwarded kinds (e.g. ``UNASSIGN``) return ``None`` so the caller stream stays minimal.
    """
    base = _base_kwargs(event)
    # ``event.kind`` may be a TextChoices/str-enum member (whose ``str()`` is "Cls.NAME") or a
    # plain string depending on how the row was built/loaded — normalize to the raw value.
    kind = event.kind.value if hasattr(event.kind, "value") else str(event.kind)

    if kind == Kind.PROGRESS.value:
        return messages.ProgressEvent(**base, progress=event.progress, message=event.message)
    if kind == Kind.YIELD.value:
        return messages.YieldEvent(**base, returns=event.returns)
    if kind == Kind.LOG.value:
        level = event.level if event.level in _LOG_LEVELS else "INFO"
        return messages.LogEvent(**base, message=event.message, level=cast(messages.LogLevelLiteral, level))
    if kind == Kind.FAILED.value:
        return messages.FailedEvent(**base, error=event.message)
    if kind == Kind.CRITICAL.value:
        return messages.CriticalEvent(**base, error=event.message)
    if kind == Kind.LOST.value:
        details = event.value if isinstance(event.value, dict) else {}
        return messages.LostEvent(
            **base,
            started=bool(details.get("started", True)),
            last_progress=details.get("last_progress"),
            effects=details.get("effects"),
            reason=details.get("reason") or event.message,
        )
    if kind == Kind.COMPLETED.value:
        return messages.CompletedEvent(**base)
    if kind == Kind.BOUND.value:
        return messages.BoundEvent(**base)
    if kind == Kind.QUEUED.value:
        return messages.QueuedEvent(**base)
    if kind == Kind.STARTED.value:
        return messages.StartedEvent(**base)
    if kind == Kind.DELEGATE.value:
        return messages.DelegateEvent(**base)
    if kind == Kind.CANCELLING.value:
        return messages.CancellingEvent(**base)
    if kind == Kind.CANCELLED.value:
        return messages.CancelledEvent(**base)
    if kind == Kind.INTERRUPTING.value:
        return messages.InterruptingEvent(**base)
    if kind == Kind.INTERRUPTED.value:
        return messages.InterruptedEvent(**base)
    if kind == Kind.PAUSING.value:
        return messages.PausingEvent(**base)
    if kind == Kind.PAUSED.value:
        return messages.PausedEvent(**base)
    if kind == Kind.RESUMING.value:
        return messages.ResumingEvent(**base)
    if kind == Kind.RESUMED.value:
        return messages.ResumedEvent(**base)
    return None
