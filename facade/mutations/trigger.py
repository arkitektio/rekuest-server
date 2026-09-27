"""Trigger mutations: create, change, delete.

Every resolver scopes itself through :func:`facade.types.base.scoped_get`. A trigger is checked
when it is written — its port takes the structure it reacts to, its args fit the action, its
conditions compile — so a broken trigger is refused once instead of failing on every signal.
"""

import strawberry
from django.conf import settings
from kante.types import Info
from rekuest_core.objects.models import ArgPortModel
from rekuest_core.values import validate_assignment_args

from facade import inputs, models, triggers, types
from facade.backend import get_caller_for_context
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def _check_args(action: models.Action, port: str, identifier: str, args: dict) -> None:
    """The fixed args plus a stand-in object in ``port`` must fit the action's ports."""
    if action.args:
        validate_assignment_args([ArgPortModel(**p) for p in action.args], {**args, port: {"__identifier": identifier, "object": "0"}})


def _check_provenance(action: models.Action, agent: models.Agent | None, interface: str | None) -> None:
    """Under strict provenance, a signal without a causing task could never get a token for such a target."""
    if not settings.PROVENANCE.get("STRICT"):
        return
    implementations = models.Implementation.objects.filter(action=action)
    if agent is not None:
        implementations = implementations.filter(agent=agent, interface=interface)
    if implementations.filter(needs_token=True).exists():
        raise ValueError("This action needs a provenance token; under strict provenance a trigger can only feed it signals caused by a task, which cannot be guaranteed")


def _check_declared(kind: str, identifier: str, conditions: list[dict]) -> None:
    """The trigger waits for something a service declares it emits, on keys that service sends."""
    declarations = list(models.SignalDeclaration.objects.filter(identifier=identifier).select_related("agent"))
    matching = [d for d in declarations if d.kind == kind]
    if not matching:
        if declarations:
            kinds = sorted({d.kind for d in declarations})
            raise ValueError(f"No service emits {kind} for {identifier}; declared: {', '.join(kinds)}")
        raise ValueError(f"No service declares signals for {identifier}")
    keys = {key for d in matching for key in d.descriptor_keys}
    unknown = sorted({c["key"] for c in conditions} - keys)
    if unknown:
        raise ValueError(f"No service sends the descriptor(s) {', '.join(unknown)} for {kind} {identifier}; sent: {', '.join(sorted(keys)) or 'none'}")


def create_trigger(info: Info, input: inputs.CreateTriggerInput) -> types.Trigger:
    action = scoped_get(models.Action, info, input.action)
    agent = None
    if (input.agent is None) != (input.interface is None):
        raise ValueError("Pin an agent with both agent and interface, or give neither")
    if input.agent is not None:
        agent = scoped_get(models.Agent, info, input.agent)
        if not models.Implementation.objects.filter(agent=agent, interface=input.interface, action=action).exists():
            raise ValueError(f"Agent {agent.pk} has no implementation {input.interface!r} of this action")

    triggers.target_port(action, input.port, input.identifier)
    args = input.args or {}
    _check_args(action, input.port, input.identifier, args)
    conditions, compiled = triggers.compile_conditions(input.conditions or [])
    _check_declared(input.kind.value, input.identifier, conditions)
    _check_provenance(action, agent, input.interface)

    return models.Trigger.objects.create(
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
    if input.name is not None:
        trigger.name = input.name
    if input.args is not None:
        _check_args(trigger.action, trigger.port, trigger.identifier, input.args)
        trigger.args = input.args
    if input.conditions is not None:
        conditions, compiled = triggers.compile_conditions(input.conditions)
        _check_declared(trigger.kind, trigger.identifier, conditions)
        trigger.conditions, trigger.compiled_jsonpath = conditions, compiled
    if input.enabled is not None:
        trigger.enabled = input.enabled
    trigger.save()
    return trigger


def delete_trigger(info: Info, input: inputs.TriggerIdInput) -> strawberry.ID:
    """Delete a trigger. Its runs are kept; their `trigger` link turns null."""
    scoped_get(models.Trigger, info, input.id, field="caller__organization").delete()
    return input.id
