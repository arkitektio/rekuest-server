"""Importing, deleting and exporting wiregrams (see :mod:`facade.models.wiregram`).

A wiregram is declarative: after an import, the rules it owns are exactly the document's. What
that takes, in order:

1. every rule's target (an agent's name, one of its interfaces) is found in the importing
   organization, and every rule is checked by the same validators a single ``createSchedule`` /
   ``createTrigger`` uses. Any failure refuses the whole document; nothing is written;
2. the waiting runs of schedules that will go or change are cancelled (takt), so none is
   handed over under the old terms;
3. in one transaction: rules are created or updated by their key, and the wiregram's rules the
   document no longer lists are deleted;
4. once that is committed, takt plans the schedules' next runs.

An organization's own switch survives: ``enabled`` is taken from the document only when a rule
is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from facade import models, rules, schedules, triggers
from facade.inputs.wiregram import WiregramModel, WireScheduleModel, WireTriggerModel


@dataclass
class _Target:
    agent: models.Agent
    action: models.Action
    interface: str


def _target(organization: Any, what: str, agent: str, interface: str) -> _Target:
    """The one implementation ``agent``.``interface`` names in ``organization``, or ``ValueError``.

    An agent's name is not unique in an organization, so none and several are both refused: a
    rule must not land on whichever agent a query happened to return first.
    """
    found = list(models.Implementation.objects.filter(agent__organization=organization, agent__name=agent, interface=interface).select_related("agent", "action", "agent__client"))
    if len(found) == 1:
        return _Target(agent=found[0].agent, action=found[0].action, interface=interface)
    if found:
        listed = ", ".join(f"agent {impl.agent_id} ({impl.agent.client.client_id})" for impl in found)
        raise ValueError(f"{what}: several agents named {agent!r} offer {interface!r} in this organization ({listed}); a wiregram cannot tell them apart")
    named = models.Agent.objects.filter(organization=organization, name=agent)
    if not named.exists():
        raise ValueError(f"{what}: this organization has no agent named {agent!r}")
    offered = sorted(set(models.Implementation.objects.filter(agent__in=named).values_list("interface", flat=True)))
    raise ValueError(f"{what}: the agent {agent!r} has no interface {interface!r}; it offers: {', '.join(offered) or 'nothing'}")


def _schedule_fields(rule: WireScheduleModel, target: _Target) -> dict[str, Any]:
    schedules.validate(action=target.action, agent=target.agent, interface=target.interface, args=rule.args, interval_seconds=rule.interval_seconds, cron=rule.cron, tz=rule.timezone)
    rules.check_policies(max_runs=rule.max_runs)
    return dict(
        name=rule.name,
        description=rule.description,
        action=target.action,
        agent=target.agent,
        interface=target.interface,
        args=rule.args,
        interval_seconds=rule.interval_seconds,
        cron=rule.cron,
        timezone=rule.timezone,
        ephemeral_runs=rule.ephemeral_runs,
        overlap=rule.overlap.value,
        catch_up=rule.catch_up,
        ends_at=rule.ends_at,
        max_runs=rule.max_runs,
    )


def _trigger_fields(rule: WireTriggerModel, target: _Target) -> dict[str, Any]:
    conditions, compiled = triggers.validate(
        action=target.action, agent=target.agent, interface=target.interface, kind=rule.kind.value, identifier=rule.identifier, port=rule.port, args=rule.args, conditions=list(rule.conditions)
    )
    rules.check_policies(max_runs=rule.max_runs, debounce_seconds=rule.debounce_seconds)
    return dict(
        name=rule.name,
        description=rule.description,
        action=target.action,
        agent=target.agent,
        interface=target.interface,
        args=rule.args,
        kind=rule.kind.value,
        identifier=rule.identifier,
        port=rule.port,
        conditions=conditions,
        compiled_jsonpath=compiled,
        debounce_seconds=rule.debounce_seconds,
        ends_at=rule.ends_at,
        max_runs=rule.max_runs,
    )


def _checked(document: WiregramModel, organization: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    """Every rule's row fields, by key. Raises one ``ValueError`` naming every rule that cannot be."""
    wanted_schedules: dict[str, dict] = {}
    wanted_triggers: dict[str, dict] = {}
    problems: list[str] = []
    for what, listed, fields, wanted in (("Schedule", document.schedules, _schedule_fields, wanted_schedules), ("Trigger", document.triggers, _trigger_fields, wanted_triggers)):
        for rule in listed:
            label = f"{what} {rule.key!r}"
            try:
                wanted[rule.key] = fields(rule, _target(organization, label, rule.agent, rule.interface))
            except ValueError as error:
                message = str(error)
                problems.append(message if message.startswith(label) else f"{label}: {message}")
    if problems:
        raise ValueError("The wiregram cannot be imported. " + " | ".join(problems))
    return wanted_schedules, wanted_triggers


def _retimed(schedule: models.Schedule, fields: dict[str, Any]) -> bool:
    """Whether the document changes what the schedule's waiting run was planned for."""
    planned = ("action", "agent", "interface", "args", "interval_seconds", "cron", "timezone", "ephemeral_runs", "ends_at", "max_runs")
    return any(getattr(schedule, name) != fields[name] for name in planned)


def import_wiregram(document: WiregramModel, caller: models.Caller, principal: Any = None) -> models.Wiregram:
    """Bring the organization's rules of this wiregram in line with ``document``; all or nothing."""
    organization = caller.organization
    wanted_schedules, wanted_triggers = _checked(document, organization)
    enabled = {("schedule", rule.key): rule.enabled for rule in document.schedules} | {("trigger", rule.key): rule.enabled for rule in document.triggers}

    existing = models.Wiregram.objects.filter(organization=organization, key=document.key).first()
    owned = {schedule.wire_key: schedule for schedule in models.Schedule.objects.filter(wiregram=existing)} if existing is not None else {}
    # Before anything is written: a schedule that goes, or whose run was planned on other terms,
    # must not have that run handed over. Should the import then fail, takt simply plans it again.
    replanned = [key for key, schedule in owned.items() if key in wanted_schedules and _retimed(schedule, wanted_schedules[key])]
    for key, schedule in owned.items():
        if key not in wanted_schedules or key in replanned:
            schedules.cancel_waiting_run(schedule, principal)

    with transaction.atomic():
        wiregram, _ = models.Wiregram.objects.update_or_create(
            organization=organization,
            key=document.key,
            defaults=dict(caller=caller, name=document.name, description=document.description, document=document.model_dump(mode="json")),
        )
        for kind, model, wanted in (("schedule", models.Schedule, wanted_schedules), ("trigger", models.Trigger, wanted_triggers)):
            model.objects.filter(wiregram=wiregram).exclude(wire_key__in=wanted).delete()
            for key, fields in wanted.items():
                rule = model.objects.filter(wiregram=wiregram, wire_key=key).first()
                if rule is None:
                    model.objects.create(wiregram=wiregram, wire_key=key, caller=caller, enabled=enabled[(kind, key)], **fields)
                    continue
                # Only the document's fields: the run bookkeeping is takt's, and `enabled` is the organization's switch.
                for name, value in fields.items():
                    setattr(rule, name, value)
                rule.save(update_fields=[*fields, "updated_at"])

    # takt cannot see uncommitted rows: plan only now.
    for schedule in models.Schedule.objects.filter(wiregram=wiregram):
        schedules.plan(schedule, principal=principal)
    return wiregram


def delete_wiregram(wiregram: models.Wiregram, principal: Any = None) -> None:
    """Remove a wiregram and the rules it owns. Waiting runs are cancelled; history is kept."""
    for schedule in models.Schedule.objects.filter(wiregram=wiregram):
        schedules.cancel_waiting_run(schedule, principal)
    wiregram.delete()


def export_wiregram(key: str, name: str, description: str | None, listed_schedules: list[models.Schedule], listed_triggers: list[models.Trigger]) -> dict[str, Any]:
    """Existing rules written down as a wiregram document, e.g. to import into another organization.

    A rule must be pinned to an agent: a wiregram names its target as agent + interface and has
    no way to say "any agent implementing this action".
    """

    def base(rule: Any, what: str) -> dict[str, Any]:
        if rule.agent_id is None:
            raise ValueError(f"{what} {rule.name!r} is not pinned to an agent; a wiregram names its target as an agent and an interface")
        return dict(
            key=rule.wire_key or f"{what.lower()}-{rule.pk}",
            name=rule.name,
            description=rule.description,
            agent=rule.agent.name,
            interface=rule.interface,
            args=rule.args or {},
            enabled=rule.enabled,
            ends_at=rule.ends_at,
            max_runs=rule.max_runs,
        )

    document = WiregramModel(
        key=key,
        name=name,
        description=description,
        schedules=[
            WireScheduleModel(
                **base(s, "Schedule"), interval_seconds=s.interval_seconds, cron=s.cron, timezone=s.timezone, ephemeral_runs=s.ephemeral_runs, overlap=s.overlap, catch_up=s.catch_up
            )
            for s in listed_schedules
        ],
        triggers=[WireTriggerModel(**base(t, "Trigger"), kind=t.kind, identifier=t.identifier, port=t.port, conditions=t.conditions or [], debounce_seconds=t.debounce_seconds) for t in listed_triggers],
    )
    return document.model_dump(mode="json")
