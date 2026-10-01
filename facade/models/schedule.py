"""Schedules: recurring assignments of one action, materialized as delayed tasks.

A schedule is never a timer. It owns at most ONE open task at a time — the next run, created
as a delayed task (``Task.not_before`` = the next slot) by the reaper's ``refill_schedules`` once
the previous run is terminal. Everything else falls out of that invariant:

* **no overlap** — the next run is only created after the previous one finished;
* **misfire = fire once** — a run whose slot passed while no reaper ticked is simply due, and
  ``dispatch_due_tasks`` hands it over on the first tick back;
* **exactly once per slot** — the run's ``reference`` is ``schedule:<id>:<slot>``, which the
  ``(caller, reference)`` unique constraint on Task makes a database guarantee;
* **skip / run now** — cancelling the waiting run skips that slot; ``triggerSchedule`` moves it
  to now.
"""

from django.db.models.functions import Now
from django.db import models


class Schedule(models.Model):
    """A recurring assignment: the action, its args, and when (an interval or a cron line)."""

    name = models.CharField(max_length=200, help_text="A human-readable name for the schedule")
    caller = models.ForeignKey(
        "Caller",
        on_delete=models.CASCADE,
        related_name="schedules",
        help_text="The identity every run is assigned as — its organization scopes the schedule, and it namespaces the runs' references",
    )
    action = models.ForeignKey("Action", on_delete=models.CASCADE, related_name="schedules", help_text="The action every run assigns")
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="schedules",
        help_text="Pin every run to this agent (with ``interface``); null = resolve an agent for the action per run",
    )
    interface = models.CharField(max_length=1000, null=True, blank=True, help_text="The implementation interface on the pinned agent")
    args = models.JSONField(default=dict, blank=True, help_text="The args every run is assigned with", db_default={})
    interval_seconds = models.PositiveIntegerField(null=True, blank=True, help_text="Run every N seconds, aligned to the schedule's creation. Exclusive with ``cron``.")
    cron = models.CharField(max_length=200, null=True, blank=True, help_text="A five-field cron line, read in ``timezone``. Exclusive with ``interval_seconds``.")
    timezone = models.CharField(max_length=64, default="UTC", help_text="The IANA zone a cron line is read in (DST included)", db_default="UTC")
    ephemeral_runs = models.BooleanField(default=False, help_text="Create the runs as ephemeral tasks (housekeeping sweeps: retention may drop them early)", db_default=False)
    enabled = models.BooleanField(default=True, help_text="A disabled schedule creates no runs", db_default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    consecutive_failures = models.PositiveIntegerField(default=0, help_text="Runs in a row that ended FAILED or CRITICAL; reset by a successful one", db_default=0)
    last_error = models.TextField(null=True, blank=True, help_text="Why the last run failed, or why the next one could not be created")
    refill_after = models.DateTimeField(null=True, blank=True, help_text="Creating the next run failed; not retried before then")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(interval_seconds__isnull=False, cron__isnull=True) | models.Q(interval_seconds__isnull=True, cron__isnull=False)),
                name="schedule_interval_xor_cron",
            ),
        ]
        indexes = [
            models.Index(fields=["caller", "-created_at"], name="schedule_caller_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.cron or f'every {self.interval_seconds}s'})"
