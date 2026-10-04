"""Schedules, as this server sees them: rows it creates and changes, runs takt plans.

A schedule is configuration (what to run, when, as whom) and this server's GraphQL owns it.
Everything about its runs is takt's. What needs an answer is asked through the internal API:
judging a timing (takt is the one reader of cron lines), its next slots, running now. A changed
row is not asked for, it is said: a ``NOTIFY`` on :data:`CHANNEL`, which Postgres delivers when
the transaction that sent it commits and drops if it rolls back. So a notice belongs inside the
transaction that writes the row, and takt never hears of a row it cannot read yet. takt's reaper
gives every enabled schedule its next run on its own; a notice only makes a change take effect
before the next tick, and nothing comes back (``nextRun`` is null until takt planned).
"""

import datetime
import uuid

from django.db import connection
from pydantic import BaseModel, Field

from facade import models, rules, takt, takt_api
from facade.caller_context import CallerContext
from facade.json_types import JSON
from rekuest_core.objects.models import ArgPortModel
from rekuest_core.values import validate_assignment_args

#: The channel takt listens on (``takt/crates/facade/src/schedule_notices.rs``).
CHANNEL = "rekuest_schedule"


class PlanNotice(BaseModel):
    """A schedule was written: plan its next run. ``replan``: its timing, target or switch
    changed, so the waiting run goes first (cancelled as ``caller``; none: the server itself)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    schedule: int
    replan: bool = False
    caller: int | None = None


class DropRunNotice(BaseModel):
    """A schedule is being deleted: drop its waiting run, named because its schedule will be gone."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run: int
    caller: int | None = None


def notify(notice: PlanNotice | DropRunNotice) -> None:
    """Send takt a notice, as part of the current transaction (at once, outside of one)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, notice.model_dump_json()])


def _caller(principal: CallerContext | None) -> int | None:
    """The caller a waiting run is cancelled as: the principal's, or none (the server itself)."""
    return principal.caller().pk if principal is not None else None


def validate_timing(*, interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Raise ``ValueError`` unless exactly one of interval/cron is set and both it and ``tz`` parse."""
    takt.call(takt_api.VALIDATE_TIMING, takt_api.Timing(interval_seconds=interval_seconds, cron=cron, timezone=tz))


def validate(*, action: models.Action, agent: models.Agent | None, interface: str | None, args: dict[str, JSON], interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Everything a schedule must satisfy to be written. One place, used when a schedule is
    created, changed or imported: its pin is an implementation of the action, its args fit, its
    timing parses."""
    rules.check_pin(action, agent, interface)
    rules.check_provenance(action, agent, interface, "This action needs a provenance token, which a scheduled run cannot get while provenance is strict")
    if action.args:
        validate_assignment_args([ArgPortModel(**port) for port in action.args], args)
    validate_timing(interval_seconds=interval_seconds, cron=cron, tz=tz)


def plan(schedule: models.Schedule, *, replan: bool = False, principal: CallerContext | None = None) -> None:
    """Have takt plan the schedule's next run once this commits. ``replan``: its timing, target or
    switch changed, so the waiting run is cancelled first (as ``principal``)."""
    notify(PlanNotice(schedule=schedule.pk, replan=replan, caller=_caller(principal)))


def drop_waiting_run(schedule: models.Schedule, principal: CallerContext | None = None) -> None:
    """Before a schedule is deleted, in the same transaction: have takt drop the run that has not
    been handed over yet. The run is named, since the row that leads to it will be gone. An
    executing run is left to finish. The row is locked first: a refill that is planning it right
    now finishes, and no later one starts, so the run named here is the last one."""
    models.Schedule.objects.select_for_update().filter(pk=schedule.pk).first()
    waiting = models.Task.objects.filter(schedule=schedule, is_done=False, dispatch_attempts=0).order_by("pk").values_list("pk", flat=True).first()
    if waiting is not None:
        notify(DropRunNotice(run=waiting, caller=_caller(principal)))


def trigger(schedule: models.Schedule) -> models.Task:
    """Run now: the waiting run is moved to now, or a one-off run is created. Refused
    (``ValueError``) while a run executes."""
    return models.Task.objects.get(pk=takt.call(takt_api.TRIGGER_SCHEDULE, takt_api.ScheduleRequest(schedule=str(schedule.pk))).task)


def upcoming(asked: list[tuple[models.Schedule, int]]) -> list[list[datetime.datetime] | ValueError]:
    """For each ``(schedule, count)``: the next ``count`` slots of its timing after now, as takt
    reads it, or why takt cannot read it. One request, however many are asked.

    What the timing says, not a promise: a disabled or ended schedule runs none of them, and with
    ``SKIP`` overlap a slot that passes while a run is open is skipped.
    """
    timings = [takt_api.UpcomingTiming(interval_seconds=schedule.interval_seconds, cron=schedule.cron, timezone=schedule.timezone, created_at=schedule.created_at, count=max(1, min(count, 50))) for schedule, count in asked]
    answer = takt.call(takt_api.UPCOMING, takt_api.UpcomingRequest(timings=timings))
    return [ValueError(slots.error) if slots.error is not None else slots.slots for slots in answer.upcoming]
