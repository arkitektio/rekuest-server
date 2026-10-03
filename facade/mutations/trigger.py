"""Trigger mutations: create, change, delete.

Every resolver scopes itself through :func:`facade.types.base.scoped_get`. A trigger is checked
when it is written (:func:`facade.triggers.validate`) — its port takes the structure it reacts
to, its args fit the action, its conditions compile — so a broken trigger is refused once
instead of failing on every signal.
"""

import strawberry
from kante.types import Info

from facade import inputs, models, rules, triggers, types
from facade.backend import get_caller_for_context
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def create_trigger(info: Info, input: inputs.CreateTriggerInput) -> types.Trigger:
    action = scoped_get(models.Action, info, input.action)
    agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
    args = input.args or {}
    conditions, compiled = triggers.validate(
        action=action, agent=agent, interface=input.interface, kind=input.kind.value, identifier=input.identifier, port=input.port, args=args, conditions=input.conditions or []
    )
    rules.check_policies(max_runs=input.max_runs, debounce_seconds=input.debounce_seconds)
    return models.Trigger.objects.create(
        description=input.description,
        ends_at=input.ends_at,
        max_runs=input.max_runs,
        debounce_seconds=input.debounce_seconds,
        name=input.name,
        caller=get_caller_for_context(CallerContext.coerce(info)),
        enabled=input.enabled,
        kind=input.kind.value,
        identifier=input.identifier,
        conditions=conditions,
        compiled_jsonpath=compiled,
        action=action,
        agent=agent,
        interface=input.interface,
        port=input.port,
        args=args,
    )


def update_trigger(info: Info, input: inputs.UpdateTriggerInput) -> types.Trigger:
    trigger = scoped_get(models.Trigger, info, input.id, field="caller__organization")
    changed: list[str] = []

    def change(field: str, value) -> None:
        if getattr(trigger, field) != value:
            setattr(trigger, field, value)
            changed.append(field)

    if input.name is not None:
        change("name", input.name)
    if input.kind is not None:
        change("kind", input.kind.value)
    if input.identifier is not None:
        change("identifier", input.identifier)
    if input.action is not None:
        change("action", scoped_get(models.Action, info, input.action))
    if input.agent is not strawberry.UNSET:
        change("agent", scoped_get(models.Agent, info, input.agent) if input.agent is not None else None)
    if input.interface is not strawberry.UNSET:
        change("interface", input.interface)
    if input.port is not None:
        change("port", input.port)
    if input.args is not None:
        change("args", input.args)
    if input.enabled is not None:
        change("enabled", input.enabled)
    for policy in ("description", "ends_at", "max_runs", "debounce_seconds"):
        if getattr(input, policy) is not strawberry.UNSET:
            change(policy, getattr(input, policy))
    rules.check_policies(max_runs=trigger.max_runs, debounce_seconds=trigger.debounce_seconds)

    # The trigger as it would be, checked as a whole: a new action with the old port, or new
    # conditions against a new kind, are judged together.
    conditions, compiled = triggers.validate(
        action=trigger.action,
        agent=trigger.agent,
        interface=trigger.interface,
        kind=trigger.kind,
        identifier=trigger.identifier,
        port=trigger.port,
        args=trigger.args,
        conditions=input.conditions if input.conditions is not None else trigger.conditions,
    )
    if (conditions, compiled) != (trigger.conditions, trigger.compiled_jsonpath):
        trigger.conditions, trigger.compiled_jsonpath = conditions, compiled
        changed += ["conditions", "compiled_jsonpath"]

    # Only what changed: the firing bookkeeping on the row (failures, last error) is takt's.
    trigger.save(update_fields=[*changed, "updated_at"])
    return trigger


def delete_trigger(info: Info, input: inputs.TriggerIdInput) -> strawberry.ID:
    """Delete a trigger. Its runs are kept; their `trigger` link turns null."""
    scoped_get(models.Trigger, info, input.id, field="caller__organization").delete()
    return input.id


def fire_trigger(info: Info, input: inputs.FireTriggerInput) -> types.Firing:
    """Fire a trigger on a stored signal by hand: a replay, logged as a firing of its own."""
    trigger = scoped_get(models.Trigger, info, input.trigger, field="caller__organization")
    signal = scoped_get(models.Signal, info, input.signal, field="organization")
    return triggers.fire(trigger, signal, info)
