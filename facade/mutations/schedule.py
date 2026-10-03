"""Schedule mutations: create, change, delete, run now.

Every resolver scopes itself through :func:`facade.types.base.scoped_get` (a single-object root
resolver bypasses ``get_queryset``). The rows are this server's; their runs are takt's
(:mod:`facade.schedules`). Creating or re-planning a schedule has takt plan its next run right
away rather than on its reaper's next tick, so ``nextRun`` is populated in the response.
"""

import strawberry
from kante.types import Info

from facade import inputs, models, schedules, types
from facade.backend import get_caller_for_context
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def _caller(info: Info) -> models.Caller:
    return get_caller_for_context(CallerContext.coerce(info))


def _schedule(info: Info, id: strawberry.ID) -> models.Schedule:
    return scoped_get(models.Schedule, info, id, field="caller__organization")


def create_schedule(info: Info, input: inputs.CreateScheduleInput) -> types.Schedule:
    action = scoped_get(models.Action, info, input.action)
    agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
    args = input.args or {}
    schedules.validate(action=action, agent=agent, interface=input.interface, args=args, interval_seconds=input.interval_seconds, cron=input.cron, tz=input.timezone)

    schedule = models.Schedule.objects.create(
        name=input.name,
        caller=_caller(info),
        action=action,
        agent=agent,
        interface=input.interface,
        args=args,
        interval_seconds=input.interval_seconds,
        cron=input.cron,
        timezone=input.timezone,
        ephemeral_runs=input.ephemeral_runs,
        enabled=input.enabled,
    )
    schedules.plan(schedule)
    return schedule


def update_schedule(info: Info, input: inputs.UpdateScheduleInput) -> types.Schedule:
    schedule = _schedule(info, input.id)
    changed: list[str] = []
    replan = False

    def change(field: str, value, *, replans: bool = True) -> None:
        nonlocal replan
        if getattr(schedule, field) != value:
            setattr(schedule, field, value)
            changed.append(field)
            replan = replan or replans

    if input.name is not None:
        change("name", input.name, replans=False)
    # A new target is a fresh start: the waiting run was planned for the old one.
    if input.action is not None:
        change("action", scoped_get(models.Action, info, input.action))
    if input.agent is not strawberry.UNSET:
        change("agent", scoped_get(models.Agent, info, input.agent) if input.agent is not None else None)
    if input.interface is not strawberry.UNSET:
        change("interface", input.interface)
    if input.ephemeral_runs is not None:
        change("ephemeral_runs", input.ephemeral_runs)
    if input.args is not None:
        change("args", input.args)
    if input.interval_seconds is not None and input.cron is not None:
        raise ValueError("Give intervalSeconds or cron, not both")
    if input.interval_seconds is not None:
        change("interval_seconds", input.interval_seconds)
        change("cron", None)
    if input.cron is not None:
        change("interval_seconds", None)
        change("cron", input.cron)
    if input.timezone is not None:
        change("timezone", input.timezone)
    if input.enabled is not None:
        change("enabled", input.enabled)
    schedules.validate(
        action=schedule.action, agent=schedule.agent, interface=schedule.interface, args=schedule.args, interval_seconds=schedule.interval_seconds, cron=schedule.cron, tz=schedule.timezone
    )

    # Only what changed: the run bookkeeping on the row (backoff, failures) is takt's.
    schedule.save(update_fields=[*changed, "updated_at"])
    # A changed target or timing is a fresh start: takt cancels the waiting run of the old
    # settings, forgets their backoff and plans anew (nothing, for a disabled schedule).
    schedules.plan(schedule, replan=replan, principal=info)
    schedule.refresh_from_db()
    return schedule


def delete_schedule(info: Info, input: inputs.ScheduleIdInput) -> strawberry.ID:
    """Delete a schedule. Its waiting run is cancelled; an executing run finishes, and the history is kept."""
    schedule = _schedule(info, input.id)
    schedules.cancel_waiting_run(schedule, principal=info)
    schedule.delete()
    return input.id


def trigger_schedule(info: Info, input: inputs.ScheduleIdInput) -> types.Task:
    """Run now: the waiting run is moved to now (it replaces its slot). Refused while a run executes."""
    return schedules.trigger(_schedule(info, input.id))
