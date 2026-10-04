"""Trigger mutations: create, change, delete.

Every resolver scopes itself through :func:`facade.types.base.scoped_get`. A trigger is checked
when it is written (:func:`facade.triggers.validate`) — its port takes the structure it reacts
to, its args fit the action, its conditions compile — so a broken trigger is refused once
instead of failing on every signal.
"""

from typing import cast

import strawberry
from kante.types import Info

from facade import inputs, models, rules, triggers, types
from facade.caller_context import CallerContext
from facade.json_types import json_object, json_value
from facade.types.base import scoped_get


def create_trigger(info: Info, input: inputs.CreateTriggerInput) -> types.Trigger:
    action = scoped_get(models.Action, info, input.action)
    agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
    args = json_object(input.args or {})
    conditions, compiled = triggers.validate(action=action, agent=agent, interface=input.interface, kind=input.kind.value, identifier=input.identifier, port=input.port, args=args, conditions=json_value(input.conditions or []))
    rules.check_policies(max_runs=input.max_runs, debounce_seconds=input.debounce_seconds)
    return cast(
        "types.Trigger",
        models.Trigger.objects.create(
            description=input.description,
            ends_at=input.ends_at,
            max_runs=input.max_runs,
            debounce_seconds=input.debounce_seconds,
            name=input.name,
            caller=CallerContext.from_info(info).caller(),
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
        ),
    )


def update_trigger(info: Info, input: inputs.UpdateTriggerInput) -> types.Trigger:
    trigger = scoped_get(models.Trigger, info, input.id, field="caller__organization")
    changed: list[str] = []
    if input.name is not None and trigger.name != input.name:
        trigger.name = input.name
        changed.append("name")
    if input.kind is not None and trigger.kind != input.kind.value:
        trigger.kind = input.kind.value
        changed.append("kind")
    if input.identifier is not None and trigger.identifier != input.identifier:
        trigger.identifier = input.identifier
        changed.append("identifier")
    if input.action is not None:
        action = scoped_get(models.Action, info, input.action)
        if trigger.action != action:
            trigger.action = action
            changed.append("action")
    if input.agent is not strawberry.UNSET:
        agent = scoped_get(models.Agent, info, input.agent) if input.agent is not None else None
        if trigger.agent != agent:
            trigger.agent = agent
            changed.append("agent")
    if input.interface is not strawberry.UNSET and trigger.interface != input.interface:
        trigger.interface = input.interface
        changed.append("interface")
    if input.port is not None and trigger.port != input.port:
        trigger.port = input.port
        changed.append("port")
    if input.args is not None and trigger.args != input.args:
        trigger.args = json_object(input.args)
        changed.append("args")
    if input.enabled is not None and trigger.enabled != input.enabled:
        trigger.enabled = input.enabled
        changed.append("enabled")
    if input.description is not strawberry.UNSET and trigger.description != input.description:
        trigger.description = input.description
        changed.append("description")
    if input.ends_at is not strawberry.UNSET and trigger.ends_at != input.ends_at:
        trigger.ends_at = input.ends_at
        changed.append("ends_at")
    if input.max_runs is not strawberry.UNSET and trigger.max_runs != input.max_runs:
        trigger.max_runs = input.max_runs
        changed.append("max_runs")
    if input.debounce_seconds is not strawberry.UNSET and trigger.debounce_seconds != input.debounce_seconds:
        trigger.debounce_seconds = input.debounce_seconds
        changed.append("debounce_seconds")
    rules.check_policies(max_runs=trigger.max_runs, debounce_seconds=trigger.debounce_seconds)

    # What it runs and what it listens for is checked only when that changed, and then as a
    # whole (a new action with the old port, new conditions against a new kind). A trigger that
    # broke since it was written — its action re-registered, its signal no longer declared —
    # can still be renamed, limited or switched off.
    if input.conditions is not None or set(changed) & {"kind", "identifier", "action", "agent", "interface", "port", "args"}:
        conditions, compiled = triggers.validate(
            action=trigger.action,
            agent=trigger.agent,
            interface=trigger.interface,
            kind=trigger.kind,
            identifier=trigger.identifier,
            port=trigger.port,
            args=trigger.args,
            conditions=json_value(input.conditions if input.conditions is not None else trigger.conditions),
        )
        if (conditions, compiled) != (trigger.conditions, trigger.compiled_jsonpath):
            trigger.conditions, trigger.compiled_jsonpath = conditions, compiled
            changed += ["conditions", "compiled_jsonpath"]

    # Only what changed: the firing bookkeeping on the row (failures, last error) is takt's.
    trigger.save(update_fields=[*changed, "updated_at"])
    return cast("types.Trigger", trigger)


def delete_trigger(info: Info, input: inputs.TriggerIdInput) -> strawberry.ID:
    """Delete a trigger. Its runs are kept; their `trigger` link turns null."""
    scoped_get(models.Trigger, info, input.id, field="caller__organization").delete()
    return input.id


def fire_trigger(info: Info, input: inputs.FireTriggerInput) -> types.Firing:
    """Fire a trigger on a stored signal by hand: a replay, logged as a firing of its own."""
    trigger = scoped_get(models.Trigger, info, input.trigger, field="caller__organization")
    signal = scoped_get(models.Signal, info, input.signal, field="organization")
    return cast("types.Firing", triggers.fire(trigger, signal, CallerContext.from_info(info)))
