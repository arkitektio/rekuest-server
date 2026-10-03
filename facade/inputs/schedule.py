"""Inputs for schedules."""

import datetime

import strawberry

from facade import enums, scalars


@strawberry.input(description="Create a schedule. Give exactly one of intervalSeconds or cron; pin an agent with agent + interface, or leave both empty to resolve one per run.")
class CreateScheduleInput:
    name: str
    action: strawberry.ID
    args: scalars.Args | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str = "UTC"
    agent: strawberry.ID | None = None
    interface: str | None = None
    ephemeral_runs: bool = False
    enabled: bool = True
    description: str | None = None
    ends_at: datetime.datetime | None = strawberry.field(default=None, description="No run is planned after this moment.")
    max_runs: int | None = strawberry.field(default=None, description="No run is planned once it created this many.")
    overlap: enums.ScheduleOverlap = strawberry.field(default=enums.ScheduleOverlap.SKIP, description="Whether a run may start while the previous one is open.")
    catch_up: bool = strawberry.field(default=False, description="Run slots missed while takt was down or a run was open, late and in order, instead of skipping them.")


@strawberry.input(description="Change a schedule. Giving intervalSeconds clears cron and vice versa. Give `agent: null` (with `interface: null`) to unpin it; give `endsAt` or `maxRuns` as null to lift it. A waiting run is re-planned; an executing one finishes first.")
class UpdateScheduleInput:
    id: strawberry.ID
    name: str | None = None
    action: strawberry.ID | None = None
    agent: strawberry.ID | None = strawberry.UNSET
    interface: str | None = strawberry.UNSET
    ephemeral_runs: bool | None = None
    args: scalars.Args | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    description: str | None = strawberry.UNSET
    ends_at: datetime.datetime | None = strawberry.UNSET
    max_runs: int | None = strawberry.UNSET
    overlap: enums.ScheduleOverlap | None = None
    catch_up: bool | None = None


@strawberry.input(description="Identify a schedule.")
class ScheduleIdInput:
    id: strawberry.ID
