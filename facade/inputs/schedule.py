"""Inputs for schedules."""

import strawberry

from facade import scalars


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


@strawberry.input(description="Change a schedule. Giving intervalSeconds clears cron and vice versa. Give `agent: null` (with `interface: null`) to unpin it. A waiting run is re-planned; an executing one finishes first.")
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


@strawberry.input(description="Identify a schedule.")
class ScheduleIdInput:
    id: strawberry.ID
