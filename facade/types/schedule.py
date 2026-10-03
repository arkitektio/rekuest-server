"""The Schedule type: a recurring assignment and its runs."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from rekuest_core import scalars as rscalars

from facade import enums, filters, models, rules
from facade.types.base import build_prescoped_queryset


@strawberry_django.type(models.Schedule, filters=filters.ScheduleFilter, ordering=filters.ScheduleOrder, pagination=True, description="A recurring assignment of one action. It owns at most one waiting run at a time: the next one, a delayed task.")
class Schedule:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the schedule.")
    name: str = strawberry_django.field(description="Human-readable name.")
    action: "Action" = strawberry_django.field(description="The action every run assigns.")
    agent: Optional["Agent"] = strawberry_django.field(description="The agent every run is pinned to, if any.")
    interface: str | None = strawberry_django.field(description="The implementation interface on the pinned agent.")
    args: rscalars.AnyDefault = strawberry_django.field(description="The args every run is assigned with.")
    interval_seconds: int | None = strawberry_django.field(description="Run every N seconds (exclusive with cron).")
    cron: str | None = strawberry_django.field(description="A five-field cron line, read in `timezone` (exclusive with intervalSeconds).")
    timezone: str = strawberry_django.field(description="The IANA zone the cron line is read in.")
    ephemeral_runs: bool = strawberry_django.field(description="Whether runs are created as ephemeral tasks.")
    enabled: bool = strawberry_django.field(description="A disabled schedule creates no runs.")
    created_at: datetime.datetime = strawberry_django.field(description="Creation timestamp.")
    updated_at: datetime.datetime = strawberry_django.field(description="Last update timestamp.")
    consecutive_failures: int = strawberry_django.field(description="Runs in a row that ended FAILED or CRITICAL.")
    last_error: str | None = strawberry_django.field(description="Why the last run failed, or why the next one could not be created.")
    caller: "Caller" = strawberry_django.field(description="The identity every run is assigned as.")
    description: str | None = strawberry_django.field(description="What the schedule is for.")
    ends_at: datetime.datetime | None = strawberry_django.field(description="No run is planned after this moment.")
    max_runs: int | None = strawberry_django.field(description="No run is planned once it created this many.")
    run_count: int = strawberry_django.field(description="Runs it created so far.")
    last_fired_at: datetime.datetime | None = strawberry_django.field(description="When it last created a run.")
    last_error_at: datetime.datetime | None = strawberry_django.field(description="When `lastError` was written.")
    overlap: enums.ScheduleOverlap = strawberry_django.field(description="Whether a run may start while the previous one is open.")
    catch_up: bool = strawberry_django.field(description="Whether slots missed during downtime are run late, in order, instead of skipped.")
    wiregram: Optional["Wiregram"] = strawberry_django.field(description="The wiregram that owns this schedule, if it was imported with one.")
    wire_key: str | None = strawberry_django.field(description="What the wiregram's document calls this schedule.")

    @strawberry_django.field(description="Whether it stopped by itself: its end passed, or it created its last allowed run.")
    def exhausted(self) -> bool:
        return rules.exhausted(self)

    @strawberry_django.field(description="The next run: the one waiting for its slot, else the newest one still executing. Null while the next run is being planned, or when disabled or ended.")
    def next_run(self) -> Optional["Task"]:
        # Waiting = not handed over yet. With ALLOW overlap several runs may be open; the waiting one is the next.
        return self.tasks.filter(is_done=False).order_by("dispatch_attempts", "-created_at").first()

    @strawberry_django.field(description="The next slots of its timing after now. What the timing says, not a promise: a disabled or ended schedule runs none, and without overlap a slot that passes while a run is open is skipped.")
    def upcoming(self, count: int = 5) -> list[datetime.datetime]:
        from facade import schedules

        return [datetime.datetime.fromisoformat(slot) for slot in schedules.upcoming(self, count)]

    @strawberry_django.field(description="When its newest run was created; null when it never ran (or its runs were since deleted by retention).")
    def last_run_at(self) -> datetime.datetime | None:
        return self.tasks.order_by("-created_at").values_list("created_at", flat=True).first()

    @strawberry_django.field(description="The most recent runs, newest first.")
    def runs(self, limit: int = 20) -> list["Task"]:
        return list(self.tasks.order_by("-created_at")[: max(0, min(limit, 200))])

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="caller__organization")
