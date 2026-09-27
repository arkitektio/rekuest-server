"""Schedule arithmetic and the refill sweep (see :mod:`facade.models.schedule`).

``refill_schedules`` is a reaper step: every enabled schedule without an open run gets its next
one, created through the ordinary assign path as a delayed task. It is safe from any number of
backends at once — each schedule is claimed under a ``skip_locked`` row lock, and the run's
``schedule:<id>:<slot>`` reference is unique per caller, so a lost race returns the winner's row
instead of creating a second run.
"""

import datetime
import logging
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.db import transaction
from django.utils import timezone

from facade import enums, inputs, models
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)

# A run that ended like this counts against ``consecutive_failures``.
_FAILED_KINDS = {enums.TaskEventKind.FAILED, enums.TaskEventKind.CRITICAL}

# Creating a run failed (action gone, args no longer fit, no agent): retry no sooner than this.
REFILL_RETRY_SECONDS = 60

# Slots whose reference already exists (a cancelled or triggered run of that very slot) are
# skipped; a schedule that keeps colliding past this many is broken, not unlucky.
_MAX_SLOT_SKIPS = 5


def validate_timing(*, interval_seconds: int | None, cron: str | None, tz: str) -> None:
    """Raise ``ValueError`` unless exactly one of interval/cron is set and both it and ``tz`` parse."""
    if (interval_seconds is None) == (cron is None):
        raise ValueError("A schedule needs exactly one of interval_seconds or cron")
    if interval_seconds is not None and interval_seconds < 1:
        raise ValueError("interval_seconds must be at least 1")
    if cron is not None and not croniter.is_valid(cron):
        raise ValueError(f"Not a valid cron line: {cron!r}")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown timezone: {tz!r}") from error


def next_slot(schedule: models.Schedule, after: datetime.datetime) -> datetime.datetime:
    """The first slot strictly after ``after`` (aware, UTC).

    Interval slots are aligned to the schedule's creation, so they do not drift with how late a
    run finished. Cron lines are read in the schedule's zone, so "0 2 * * *" stays 02:00 local
    across DST changes.
    """
    if schedule.cron:
        local = after.astimezone(ZoneInfo(schedule.timezone))
        return croniter(schedule.cron, local).get_next(datetime.datetime).astimezone(datetime.timezone.utc)
    interval = datetime.timedelta(seconds=schedule.interval_seconds)
    anchor = schedule.created_at
    if after < anchor:
        return anchor
    elapsed = (after - anchor) // interval
    return anchor + (elapsed + 1) * interval


def _context(schedule: models.Schedule) -> CallerContext:
    caller = schedule.caller
    return CallerContext(user=caller.user, client=caller.client, organization=caller.organization, roles=[])


def _assign_input(schedule: models.Schedule, *, reference: str, not_before: datetime.datetime | None) -> inputs.AssignInputModel:
    if schedule.agent_id is not None:
        target = {"agent": str(schedule.agent_id), "interface": schedule.interface}
    else:
        target = {"action": str(schedule.action_id)}
    return inputs.AssignInputModel(**target, args=schedule.args or {}, reference=reference, not_before=not_before)


def _open_run(schedule: models.Schedule) -> models.Task | None:
    return schedule.tasks.filter(is_done=False).first()


def refill_one(schedule_id: int) -> bool:
    """Give one schedule its next run, if it is due one. True when a run was created."""
    from facade.backend import controll_backend  # lazy: backend imports the models graph

    now = timezone.now()
    with transaction.atomic():
        schedule = (
            models.Schedule.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("caller__user", "caller__client", "caller__organization")
            .filter(pk=schedule_id, enabled=True)
            .first()
        )
        if schedule is None or _open_run(schedule) is not None:
            return False
        if schedule.refill_after is not None and schedule.refill_after > now:
            return False

        last = schedule.tasks.order_by("-created_at").first()
        after = now
        try:
            for _ in range(_MAX_SLOT_SKIPS):
                slot = next_slot(schedule, after)
                reference = f"schedule:{schedule.pk}:{slot.isoformat()}"
                with transaction.atomic():
                    _, created = controll_backend.assign_with_status(
                        _context(schedule),
                        _assign_input(schedule, reference=reference, not_before=slot),
                        schedule=schedule,
                    )
                if created:
                    break
                after = slot  # this slot already has its run (cancelled / triggered): the next one
            else:
                raise ValueError(f"{_MAX_SLOT_SKIPS} consecutive slots already had a run")
        except Exception as error:  # the schedule is fine; its target is not — record, retry later
            schedule.last_error = f"Could not create the next run: {error}"
            schedule.refill_after = now + datetime.timedelta(seconds=REFILL_RETRY_SECONDS)
            schedule.save(update_fields=["last_error", "refill_after", "updated_at"])
            logger.warning("Schedule %s: could not create the next run: %s", schedule.pk, error)
            return False

        # Counted here, once per finished run: this is the only moment a run is known terminal
        # AND the next one exists, so a retried refill never counts the same run twice.
        if last is not None and last.latest_event_kind in _FAILED_KINDS:
            schedule.consecutive_failures += 1
            schedule.last_error = _run_error(last)
        elif last is not None:
            schedule.consecutive_failures = 0
            schedule.last_error = None
        schedule.refill_after = None
        schedule.save(update_fields=["consecutive_failures", "last_error", "refill_after", "updated_at"])
        return True


def _run_error(task: models.Task) -> str:
    event = task.events.filter(kind=task.latest_event_kind).order_by("-id").first()
    message = event.message if event is not None and event.message else ""
    return f"Run {task.pk} ended {task.latest_event_kind}" + (f": {message}" if message else "")


def refill_schedules_sync(limit: int = 100) -> int:
    """One pass over the schedules that have no open run. Returns how many got one."""
    now = timezone.now()
    candidates = list(
        models.Schedule.objects.filter(enabled=True)
        .exclude(refill_after__gt=now)
        .exclude(tasks__is_done=False)
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    return sum(1 for pk in candidates if refill_one(pk))


def cancel_waiting_run(schedule: models.Schedule, caller: models.Caller | None = None) -> None:
    """Drop a run that has not been handed over yet, so the next refill re-plans it.

    Used when a schedule is disabled, retimed or retargeted. A run that is already executing is
    left alone — it finishes, and the refill after it uses the new settings.
    """
    from facade.backend import controll_backend

    run = _open_run(schedule)
    if run is not None and run.dispatch_attempts == 0:
        controll_backend.cancel(inputs.CancelInputModel(task=str(run.pk)), caller=caller)


def trigger(schedule: models.Schedule) -> models.Task:
    """Run now. The waiting run is moved to now; with no open run, a one-off run is created.

    The moved run *replaces* its slot: the refill after it plans the slot following that one.
    A run that is already executing is not doubled — the refusal is the overlap guarantee.
    """
    from facade.backend import controll_backend

    with transaction.atomic():
        locked = models.Schedule.objects.select_for_update(of=("self",)).select_related("caller__user", "caller__client", "caller__organization").get(pk=schedule.pk)
        open_run = _open_run(locked)
        if open_run is not None:
            run = models.Task.objects.select_for_update(of=("self",)).get(pk=open_run.pk)
            if run.is_done or run.dispatch_attempts != 0:
                raise ValueError("A run of this schedule is already executing")
            # The waiting run keeps its identity (and its slot's reference) — only its time moves.
            # ``save`` rather than ``update``: the change feeds carry ``not_before``.
            run.not_before = timezone.now()
            run.save(update_fields=["not_before"])
            return run
        task, _ = controll_backend.assign_with_status(
            _context(locked),
            _assign_input(locked, reference=f"schedule:{locked.pk}:manual:{uuid.uuid4().hex}", not_before=None),
            schedule=locked,
        )
        return task
