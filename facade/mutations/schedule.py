"""Schedule mutations: create, change, delete, run now.

Every resolver scopes itself through :func:`facade.types.base.scoped_get` (a single-object root
resolver bypasses ``get_queryset``). The rows are this server's; their runs are agentd's
(:mod:`facade.schedules`). Creating or re-planning a schedule has agentd plan its next run right
away rather than on its reaper's next tick, so ``nextRun`` is populated in the response.
"""

import strawberry
from django.conf import settings
from kante.types import Info
from rekuest_core.objects.models import ArgPortModel
from rekuest_core.values import validate_assignment_args

from facade import inputs, models, schedules, types
from facade.backend import get_caller_for_context
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def _caller(info: Info) -> models.Caller:
    return get_caller_for_context(CallerContext.coerce(info))


def _check_args(action: models.Action, args: dict) -> None:
    if action.args:
        validate_assignment_args([ArgPortModel(**port) for port in action.args], args)


def _check_provenance(action: models.Action, agent: models.Agent | None, interface: str | None) -> None:
    """Refuse targets whose runs could never get a provenance token.

    A run is created by the reaper, with no human request behind it; a strict provenance policy
    refuses to mint for that, so every run of such a schedule would fail. Say so now, once.
    """
    if not settings.PROVENANCE.get("STRICT"):
        return
    implementations = models.Implementation.objects.filter(action=action)
    if agent is not None:
        implementations = implementations.filter(agent=agent, interface=interface)
    if implementations.filter(needs_token=True).exists():
        raise ValueError("This action needs a provenance token, which a scheduled run cannot get while provenance is strict")


def _schedule(info: Info, id: strawberry.ID) -> models.Schedule:
    return scoped_get(models.Schedule, info, id, field="caller__organization")


def create_schedule(info: Info, input: inputs.CreateScheduleInput) -> types.Schedule:
    action = scoped_get(models.Action, info, input.action)
    agent = None
    if (input.agent is None) != (input.interface is None):
        raise ValueError("Pin an agent with both agent and interface, or give neither")
    if input.agent is not None:
        agent = scoped_get(models.Agent, info, input.agent)
        if not models.Implementation.objects.filter(agent=agent, interface=input.interface, action=action).exists():
            raise ValueError(f"Agent {agent.pk} has no implementation {input.interface!r} of this action")

    _check_provenance(action, agent, input.interface)
    args = input.args or {}
    _check_args(action, args)
    schedules.validate_timing(interval_seconds=input.interval_seconds, cron=input.cron, tz=input.timezone)

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
    if input.name is not None:
        schedule.name = input.name
        changed.append("name")
    if input.args is not None:
        _check_args(schedule.action, input.args)
        schedule.args = input.args
        changed.append("args")
        replan = True
    if input.interval_seconds is not None and input.cron is not None:
        raise ValueError("Give intervalSeconds or cron, not both")
    if input.interval_seconds is not None:
        schedule.interval_seconds, schedule.cron = input.interval_seconds, None
        changed += ["interval_seconds", "cron"]
        replan = True
    if input.cron is not None:
        schedule.interval_seconds, schedule.cron = None, input.cron
        changed += ["interval_seconds", "cron"]
        replan = True
    if input.timezone is not None:
        schedule.timezone = input.timezone
        changed.append("timezone")
        replan = True
    if input.enabled is not None and input.enabled != schedule.enabled:
        schedule.enabled = input.enabled
        changed.append("enabled")
        replan = True
    schedules.validate_timing(interval_seconds=schedule.interval_seconds, cron=schedule.cron, tz=schedule.timezone)

    # Only what changed: the run bookkeeping on the row (backoff, failures) is agentd's.
    schedule.save(update_fields=[*changed, "updated_at"])
    # A changed target or timing is a fresh start: agentd cancels the waiting run of the old
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
