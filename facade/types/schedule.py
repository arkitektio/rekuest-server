"""The Schedule type: a recurring assignment and its runs."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from rekuest_core import scalars as rscalars

from facade import filters, models
from facade.types.base import build_prescoped_queryset


@strawberry_django.type(models.Schedule, filters=filters.ScheduleFilter, ordering=filters.ScheduleOrder, pagination=True, description="A recurring assignment of one action. It owns at most one open run at a time: the next one, a delayed task created once the previous run finished.")
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

    @strawberry_django.field(description="The open run: waiting for its slot, or executing. Null while the next run is being planned, or when disabled.")
    def next_run(self) -> Optional["Task"]:
        return self.tasks.filter(is_done=False).first()

    @strawberry_django.field(description="When its newest run was created; null when it never ran (or its runs were since deleted by retention).")
    def last_run_at(self) -> datetime.datetime | None:
        return self.tasks.order_by("-created_at").values_list("created_at", flat=True).first()

    @strawberry_django.field(description="The most recent runs, newest first.")
    def runs(self, limit: int = 20) -> list["Task"]:
        return list(self.tasks.order_by("-created_at")[: max(0, min(limit, 200))])

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="caller__organization")
