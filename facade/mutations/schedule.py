"""Schedule mutations: create, change, delete, run now.

Every resolver scopes itself through :func:`facade.types.base.scoped_get` (a single-object root
resolver bypasses ``get_queryset``). The rows are this server's; their runs are takt's
(:mod:`facade.schedules`). A created or changed schedule is said to takt in the transaction that
writes it, so its next run is planned as soon as that commits rather than on the reaper's next
tick. Nothing waits for it: ``nextRun`` in the response is what was there before takt heard.
"""

from typing import cast

import strawberry
from django.db import transaction
from kante.types import Info

from facade import inputs, models, rules, schedules, types
from facade.caller_context import CallerContext
from facade.json_types import json_object
from facade.types.base import scoped_get


def _caller(info: Info) -> models.Caller:
    return CallerContext.from_info(info).caller()


def _schedule(info: Info, id: strawberry.ID) -> models.Schedule:
    return scoped_get(models.Schedule, info, id, field="caller__organization")


def create_schedule(info: Info, input: inputs.CreateScheduleInput) -> types.Schedule:
    action = scoped_get(models.Action, info, input.action)
    agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
    args = json_object(input.args or {})
    schedules.validate(action=action, agent=agent, interface=input.interface, args=args, interval_seconds=input.interval_seconds, cron=input.cron, tz=input.timezone)

    rules.check_policies(max_runs=input.max_runs)
    with transaction.atomic():
        schedule = models.Schedule.objects.create(
            description=input.description,
            ends_at=input.ends_at,
            max_runs=input.max_runs,
            overlap=input.overlap.value,
            catch_up=input.catch_up,
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
    return cast("types.Schedule", schedule)


def update_schedule(info: Info, input: inputs.UpdateScheduleInput) -> types.Schedule:
    schedule = _schedule(info, input.id)
    changed: list[str] = []
    if input.name is not None and schedule.name != input.name:
        schedule.name = input.name
        changed.append("name")
    if input.action is not None:
        action = scoped_get(models.Action, info, input.action)
        if schedule.action != action:
            schedule.action = action
            changed.append("action")
    if input.agent is not strawberry.UNSET:
        agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
        if schedule.agent != agent:
            schedule.agent = agent
            changed.append("agent")
    if input.interface is not strawberry.UNSET and schedule.interface != input.interface:
        schedule.interface = input.interface
        changed.append("interface")
    if input.ephemeral_runs is not None and schedule.ephemeral_runs != input.ephemeral_runs:
        schedule.ephemeral_runs = input.ephemeral_runs
        changed.append("ephemeral_runs")
    if input.args is not None and schedule.args != input.args:
        schedule.args = json_object(input.args)
        changed.append("args")
    if input.interval_seconds is not None and input.cron is not None:
        raise ValueError("Give intervalSeconds or cron, not both")
    # One timing replaces the other.
    if input.interval_seconds is not None or input.cron is not None:
        if schedule.interval_seconds != input.interval_seconds:
            schedule.interval_seconds = input.interval_seconds
            changed.append("interval_seconds")
        if schedule.cron != input.cron:
            schedule.cron = input.cron
            changed.append("cron")
    if input.timezone is not None and schedule.timezone != input.timezone:
        schedule.timezone = input.timezone
        changed.append("timezone")
    if input.enabled is not None and schedule.enabled != input.enabled:
        schedule.enabled = input.enabled
        changed.append("enabled")
    if input.description is not strawberry.UNSET and schedule.description != input.description:
        schedule.description = input.description
        changed.append("description")
    if input.ends_at is not strawberry.UNSET and schedule.ends_at != input.ends_at:
        schedule.ends_at = input.ends_at
        changed.append("ends_at")
    if input.max_runs is not strawberry.UNSET and schedule.max_runs != input.max_runs:
        schedule.max_runs = input.max_runs
        changed.append("max_runs")
    if input.overlap is not None and schedule.overlap != input.overlap.value:
        schedule.overlap = input.overlap.value
        changed.append("overlap")
    if input.catch_up is not None and schedule.catch_up != input.catch_up:
        schedule.catch_up = input.catch_up
        changed.append("catch_up")
    # A new target, timing or switch is a fresh start, and a new end or limit may end the
    # schedule now: the waiting run was planned on the old terms. What it is called, and how
    # its runs overlap or catch up, says nothing about that run.
    replan = bool(set(changed) - {"name", "description", "overlap", "catch_up"})
    rules.check_policies(max_runs=schedule.max_runs)
    # What it runs and when is checked only when that changed, and then as a whole. A schedule
    # that broke since it was written (its implementation re-registered away) can still be
    # renamed, limited or switched off.
    if set(changed) & {"action", "agent", "interface", "args", "interval_seconds", "cron", "timezone"}:
        schedules.validate(action=schedule.action, agent=schedule.agent, interface=schedule.interface, args=schedule.args, interval_seconds=schedule.interval_seconds, cron=schedule.cron, tz=schedule.timezone)

    with transaction.atomic():
        # Only what changed: the run bookkeeping on the row (backoff, failures) is takt's.
        schedule.save(update_fields=[*changed, "updated_at"])
        # A changed target or timing is a fresh start: takt cancels the waiting run of the old
        # settings, forgets their backoff and plans anew (nothing, for a disabled schedule).
        schedules.plan(schedule, replan=replan, principal=CallerContext.from_info(info))
    return cast("types.Schedule", schedule)


def delete_schedule(info: Info, input: inputs.ScheduleIdInput) -> strawberry.ID:
    """Delete a schedule. Its waiting run is cancelled; an executing run finishes, and the history is kept."""
    schedule = _schedule(info, input.id)
    with transaction.atomic():
        schedules.drop_waiting_run(schedule, principal=CallerContext.from_info(info))
        schedule.delete()
    return input.id


def trigger_schedule(info: Info, input: inputs.ScheduleIdInput) -> types.Task:
    """Run now: the waiting run is moved to now (it replaces its slot). Refused while a run executes."""
    return cast("types.Task", schedules.trigger(_schedule(info, input.id)))
