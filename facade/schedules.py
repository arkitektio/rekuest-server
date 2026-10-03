"""Schedules, as this server sees them: rows it creates and changes, runs takt plans.

A schedule is configuration (what to run, when, as whom) and this server's GraphQL owns it.
Everything about its runs is takt's, asked for through the internal API: judging a timing
(takt is the one reader of cron lines), planning the next run, cancelling the waiting one,
running now. takt's reaper gives every enabled schedule its next run on its own; the calls
here only make a change take effect before the next tick.
"""

from typing import Any

from facade import models, rules, takt


def validate_timing(*, interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Raise ``ValueError`` unless exactly one of interval/cron is set and both it and ``tz`` parse."""
    takt.call("schedule/validate", {"interval_seconds": interval_seconds, "cron": cron, "timezone": tz})


def validate(*, action: models.Action, agent: models.Agent | None, interface: str | None, args: dict, interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Everything a schedule must satisfy to be written. One place, used when a schedule is
    created, changed or imported: its pin is an implementation of the action, its args fit, its
    timing parses."""
    from rekuest_core.objects.models import ArgPortModel
    from rekuest_core.values import validate_assignment_args

    rules.check_pin(action, agent, interface)
    rules.check_provenance(action, agent, interface, "This action needs a provenance token, which a scheduled run cannot get while provenance is strict")
    if action.args:
        validate_assignment_args([ArgPortModel(**port) for port in action.args], args)
    validate_timing(interval_seconds=interval_seconds, cron=cron, tz=tz)


def _request(schedule: models.Schedule, principal: Any = None, **extra: Any) -> dict[str, Any]:
    request: dict[str, Any] = {"schedule": str(schedule.pk), **extra}
    if principal is not None:
        request["principal"] = takt._principal(principal)
    return request


def plan(schedule: models.Schedule, *, replan: bool = False, principal: Any = None) -> bool:
    """Plan the schedule's next run now. ``replan``: its timing, target or switch changed, so the
    waiting run is cancelled first (as ``principal``). True when a run was created."""
    return bool(takt.call("schedule/plan", _request(schedule, principal, replan=replan))["planned"])


def cancel_waiting_run(schedule: models.Schedule, principal: Any = None) -> None:
    """Drop a run that has not been handed over yet. An executing run is left to finish."""
    takt.call("schedule/cancel-waiting", _request(schedule, principal))


def trigger(schedule: models.Schedule) -> models.Task:
    """Run now: the waiting run is moved to now, or a one-off run is created. Refused
    (``ValueError``) while a run executes."""
    return models.Task.objects.get(pk=takt.call("schedule/trigger", _request(schedule))["task"])


def upcoming(schedule: models.Schedule, count: int = 5) -> list[str]:
    """The next ``count`` slots of the schedule's timing after now, as takt reads it (ISO, UTC).

    What the timing says, not a promise: a disabled or ended schedule runs none of them, and with
    ``SKIP`` overlap a slot that passes while a run is open is skipped.
    """
    request = {"interval_seconds": schedule.interval_seconds, "cron": schedule.cron, "timezone": schedule.timezone, "created_at": schedule.created_at.isoformat(), "count": max(1, min(count, 50))}
    return list(takt.call("schedule/upcoming", request)["slots"])
