"""Schedules, as this server sees them: rows it creates and changes, runs agentd plans.

A schedule is configuration (what to run, when, as whom) and this server's GraphQL owns it.
Everything about its runs is agentd's, asked for through the internal API: judging a timing
(agentd is the one reader of cron lines), planning the next run, cancelling the waiting one,
running now. agentd's reaper gives every enabled schedule its next run on its own; the calls
here only make a change take effect before the next tick.
"""

from typing import Any

from facade import agentd, models


def validate_timing(*, interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Raise ``ValueError`` unless exactly one of interval/cron is set and both it and ``tz`` parse."""
    agentd.call("schedule/validate", {"interval_seconds": interval_seconds, "cron": cron, "timezone": tz})


def _request(schedule: models.Schedule, principal: Any = None, **extra: Any) -> dict[str, Any]:
    request: dict[str, Any] = {"schedule": str(schedule.pk), **extra}
    if principal is not None:
        request["principal"] = agentd._principal(principal)
    return request


def plan(schedule: models.Schedule, *, replan: bool = False, principal: Any = None) -> bool:
    """Plan the schedule's next run now. ``replan``: its timing, target or switch changed, so the
    waiting run is cancelled first (as ``principal``). True when a run was created."""
    return bool(agentd.call("schedule/plan", _request(schedule, principal, replan=replan))["planned"])


def cancel_waiting_run(schedule: models.Schedule, principal: Any = None) -> None:
    """Drop a run that has not been handed over yet. An executing run is left to finish."""
    agentd.call("schedule/cancel-waiting", _request(schedule, principal))


def trigger(schedule: models.Schedule) -> models.Task:
    """Run now: the waiting run is moved to now, or a one-off run is created. Refused
    (``ValueError``) while a run executes."""
    return models.Task.objects.get(pk=agentd.call("schedule/trigger", _request(schedule))["task"])
