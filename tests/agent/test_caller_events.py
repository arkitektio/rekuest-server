"""Unit tests for the pure caller-event mapper (no DB, no channels).

``build_execution_event`` is the single place that turns a persisted ``TaskEvent``
into its minimal caller-bound socket message, so it is exercised here with a light stub.
"""

from dataclasses import dataclass
from typing import Optional

import pytest

from facade import messages
from facade.caller_events import build_execution_event
from facade.enums import TaskEventChoices as Kind


@dataclass
class StubEvent:
    """The minimal surface ``build_execution_event`` reads — stands in for the ORM model."""

    id: int = 42
    task_id: str = "ass-1"
    kind: str = Kind.PROGRESS.value
    message: Optional[str] = None
    progress: Optional[int] = None
    returns: Optional[dict] = None
    level: Optional[str] = None
    value: Optional[object] = None


def test_progress_maps_with_progress_and_message():
    msg = build_execution_event(StubEvent(kind=Kind.PROGRESS.value, progress=50, message="half"))
    assert isinstance(msg, messages.ProgressEvent)
    assert msg.task == "ass-1" and msg.event == "42" and msg.seq == 42
    assert msg.progress == 50 and msg.message == "half"


def test_yield_carries_returns():
    msg = build_execution_event(StubEvent(kind=Kind.YIELD.value, returns={"x": 1}))
    assert isinstance(msg, messages.YieldEvent) and msg.returns == {"x": 1}


def test_error_and_critical_take_message_as_error():
    err = build_execution_event(StubEvent(kind=Kind.FAILED.value, message="boom"))
    crit = build_execution_event(StubEvent(kind=Kind.CRITICAL.value, message="fatal"))
    assert isinstance(err, messages.FailedEvent) and err.error == "boom"
    assert isinstance(crit, messages.CriticalEvent) and crit.error == "fatal"


def test_log_defaults_invalid_level_to_info():
    # TaskEvent.level is an TaskEventChoices string (or None) — not a log
    # level — so a non-log-level value must degrade to INFO rather than fail validation.
    info = build_execution_event(StubEvent(kind=Kind.LOG.value, message="hi", level=None))
    assert isinstance(info, messages.LogEvent) and info.level == "INFO" and info.message == "hi"
    warn = build_execution_event(StubEvent(kind=Kind.LOG.value, message="hi", level="WARN"))
    assert isinstance(warn, messages.LogEvent) and warn.level == "WARN"
    coerced = build_execution_event(StubEvent(kind=Kind.LOG.value, level="PROGRESS"))
    assert isinstance(coerced, messages.LogEvent) and coerced.level == "INFO"


def test_lost_carries_what_is_known():
    msg = build_execution_event(
        StubEvent(
            kind=Kind.LOST.value,
            message="Its agent died while it ran; how it ended is unknown.",
            value={"started": True, "last_progress": 60, "effects": "IRREVERSIBLE", "reason": "Its agent died while it ran; how it ended is unknown."},
        )
    )
    assert isinstance(msg, messages.LostEvent)
    assert (msg.started, msg.last_progress, msg.effects) == (True, 60, "IRREVERSIBLE")
    assert msg.reason == "Its agent died while it ran; how it ended is unknown."


def test_lost_that_never_started_says_so():
    msg = build_execution_event(StubEvent(kind=Kind.LOST.value, value={"started": False, "last_progress": None, "effects": "UNKNOWN", "reason": "never picked up"}))
    assert isinstance(msg, messages.LostEvent) and msg.started is False and msg.last_progress is None


def test_disconnected_carries_message():
    msg = build_execution_event(StubEvent(kind=Kind.DISCONNECTED.value, message="fate unknown"))
    assert isinstance(msg, messages.DisconnectedEvent) and msg.message == "fate unknown"


@pytest.mark.parametrize(
    "kind,cls",
    [
        (Kind.BOUND.value, messages.BoundEvent),
        (Kind.QUEUED.value, messages.QueuedEvent),
        (Kind.STARTED.value, messages.StartedEvent),
        (Kind.DELEGATE.value, messages.DelegateEvent),
        (Kind.COMPLETED.value, messages.CompletedEvent),
        (Kind.CANCELLING.value, messages.CancellingEvent),
        (Kind.CANCELLED.value, messages.CancelledEvent),
        (Kind.INTERRUPTING.value, messages.InterruptingEvent),
        (Kind.INTERRUPTED.value, messages.InterruptedEvent),
        (Kind.PAUSING.value, messages.PausingEvent),
        (Kind.PAUSED.value, messages.PausedEvent),
        (Kind.RESUMING.value, messages.ResumingEvent),
        (Kind.RESUMED.value, messages.ResumedEvent),
    ],
)
def test_bare_kinds_map_to_their_class(kind, cls):
    msg = build_execution_event(StubEvent(kind=kind))
    assert isinstance(msg, cls)
    assert msg.task == "ass-1" and msg.event == "42" and msg.seq == 42


def test_interrupted_is_a_plain_string_value_not_a_tuple():
    # Guards against a `INTERRUPTED = ("INTERRUPTED",)` tuple literal in TaskEventKind:
    # the persisted choices value must be a plain string so the mapping matches.
    assert Kind.INTERRUPTED.value == "INTERRUPTED"
    assert isinstance(build_execution_event(StubEvent(kind="INTERRUPTED")), messages.InterruptedEvent)


def test_unforwarded_kind_returns_none():
    assert build_execution_event(StubEvent(kind="UNASSIGN")) is None
