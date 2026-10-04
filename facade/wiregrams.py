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

import datetime
from dataclasses import dataclass
from typing import Protocol

from authentikate.models import Organization
from django.db import transaction

from facade import models, rules, schedules, triggers
from facade.caller_context import CallerContext
from facade.inputs.wiregram import WiregramModel, WireScheduleModel, WireTriggerModel
from facade.json_types import JSON, JSONObject


@dataclass
class _Target:
    agent: models.Agent
    action: models.Action
    interface: str


def _target(organization: Organization, what: str, agent: str, interface: str) -> _Target:
    """The one implementation ``agent``.``interface`` names in ``organization``, or ``ValueError``.

    An agent's name is not unique in an organization, so none and several are both refused: a
    rule must not land on whichever agent a query happened to return first.
    """
    found = list(models.Implementation.objects.filter(agent__organization=organization, agent__name=agent, interface=interface).select_related("agent", "action", "agent__client"))
    if len(found) == 1:
        return _Target(agent=found[0].agent, action=found[0].action, interface=interface)
    if found:
        listed = ", ".join(f"agent {impl.agent.pk} ({impl.agent.client.client_id})" for impl in found)
        raise ValueError(f"{what}: several agents named {agent!r} offer {interface!r} in this organization ({listed}); a wiregram cannot tell them apart")
    named = models.Agent.objects.filter(organization=organization, name=agent)
    if not named.exists():
        raise ValueError(f"{what}: this organization has no agent named {agent!r}")
    offered = sorted(set(models.Implementation.objects.filter(agent__in=named).values_list("interface", flat=True)))
    raise ValueError(f"{what}: the agent {agent!r} has no interface {interface!r}; it offers: {', '.join(offered) or 'nothing'}")


class _Fields[Rule: models.Schedule | models.Trigger](Protocol):
    """What a document says a rule's row holds. Not its bookkeeping (takt's), nor ``enabled``
    (the organization's own switch, taken from the document only when the rule is created)."""

    def create(self, wiregram: models.Wiregram, key: str, caller: models.Caller, enabled: bool) -> Rule: ...

    def update(self, rule: Rule) -> None: ...


@dataclass(frozen=True)
class _ScheduleFields:
    name: str
    description: str | None
    target: _Target
    args: dict[str, JSON]
    interval_seconds: int | None
    cron: str | None
    timezone: str
    ephemeral_runs: bool
    overlap: str
    catch_up: bool
    ends_at: datetime.datetime | None
    max_runs: int | None

    @classmethod
    def of(cls, rule: WireScheduleModel, target: _Target) -> _ScheduleFields:
        """The row a document's schedule asks for, checked as a single ``createSchedule`` is."""
        args = rule.args or {}
        schedules.validate(action=target.action, agent=target.agent, interface=target.interface, args=args, interval_seconds=rule.interval_seconds, cron=rule.cron, tz=rule.timezone)
        rules.check_policies(max_runs=rule.max_runs)
        return cls(
            name=rule.name,
            description=rule.description,
            target=target,
            args=args,
            interval_seconds=rule.interval_seconds,
            cron=rule.cron,
            timezone=rule.timezone,
            ephemeral_runs=rule.ephemeral_runs,
            overlap=rule.overlap.value,
            catch_up=rule.catch_up,
            ends_at=rule.ends_at,
            max_runs=rule.max_runs,
        )

    def retimes(self, schedule: models.Schedule) -> bool:
        """Whether this changes what the schedule's waiting run was planned for."""
        planned = (schedule.action, schedule.agent, schedule.interface, schedule.args, schedule.interval_seconds, schedule.cron, schedule.timezone, schedule.ephemeral_runs, schedule.ends_at, schedule.max_runs)
        wanted = (self.target.action, self.target.agent, self.target.interface, self.args, self.interval_seconds, self.cron, self.timezone, self.ephemeral_runs, self.ends_at, self.max_runs)
        return planned != wanted

    def _set(self, schedule: models.Schedule) -> None:
        schedule.name = self.name
        schedule.description = self.description
        schedule.action = self.target.action
        schedule.agent = self.target.agent
        schedule.interface = self.target.interface
        schedule.args = self.args
        schedule.interval_seconds = self.interval_seconds
        schedule.cron = self.cron
        schedule.timezone = self.timezone
        schedule.ephemeral_runs = self.ephemeral_runs
        schedule.overlap = self.overlap
        schedule.catch_up = self.catch_up
        schedule.ends_at = self.ends_at
        schedule.max_runs = self.max_runs

    def create(self, wiregram: models.Wiregram, key: str, caller: models.Caller, enabled: bool) -> models.Schedule:
        schedule = models.Schedule(wiregram=wiregram, wire_key=key, caller=caller, enabled=enabled)
        self._set(schedule)
        schedule.save()
        return schedule

    def update(self, rule: models.Schedule) -> None:
        self._set(rule)
        rule.save(update_fields=["name", "description", "action", "agent", "interface", "args", "interval_seconds", "cron", "timezone", "ephemeral_runs", "overlap", "catch_up", "ends_at", "max_runs", "updated_at"])


@dataclass(frozen=True)
class _TriggerFields:
    name: str
    description: str | None
    target: _Target
    args: dict[str, JSON]
    kind: str
    identifier: str
    port: str
    conditions: list[JSONObject]
    compiled_jsonpath: str | None
    debounce_seconds: int | None
    ends_at: datetime.datetime | None
    max_runs: int | None

    @classmethod
    def of(cls, rule: WireTriggerModel, target: _Target) -> _TriggerFields:
        """The row a document's trigger asks for, checked as a single ``createTrigger`` is."""
        args = rule.args or {}
        conditions, compiled = triggers.validate(action=target.action, agent=target.agent, interface=target.interface, kind=rule.kind.value, identifier=rule.identifier, port=rule.port, args=args, conditions=rule.conditions)
        rules.check_policies(max_runs=rule.max_runs, debounce_seconds=rule.debounce_seconds)
        return cls(
            name=rule.name,
            description=rule.description,
            target=target,
            args=args,
            kind=rule.kind.value,
            identifier=rule.identifier,
            port=rule.port,
            conditions=conditions,
            compiled_jsonpath=compiled,
            debounce_seconds=rule.debounce_seconds,
            ends_at=rule.ends_at,
            max_runs=rule.max_runs,
        )

    def _set(self, trigger: models.Trigger) -> None:
        trigger.name = self.name
        trigger.description = self.description
        trigger.action = self.target.action
        trigger.agent = self.target.agent
        trigger.interface = self.target.interface
        trigger.args = self.args
        trigger.kind = self.kind
        trigger.identifier = self.identifier
        trigger.port = self.port
        trigger.conditions = self.conditions
        trigger.compiled_jsonpath = self.compiled_jsonpath
        trigger.debounce_seconds = self.debounce_seconds
        trigger.ends_at = self.ends_at
        trigger.max_runs = self.max_runs

    def create(self, wiregram: models.Wiregram, key: str, caller: models.Caller, enabled: bool) -> models.Trigger:
        trigger = models.Trigger(wiregram=wiregram, wire_key=key, caller=caller, enabled=enabled)
        self._set(trigger)
        trigger.save()
        return trigger

    def update(self, rule: models.Trigger) -> None:
        self._set(rule)
        rule.save(update_fields=["name", "description", "action", "agent", "interface", "args", "kind", "identifier", "port", "conditions", "compiled_jsonpath", "debounce_seconds", "ends_at", "max_runs", "updated_at"])


def _checked(document: WiregramModel, organization: Organization) -> tuple[dict[str, _ScheduleFields], dict[str, _TriggerFields]]:
    """Every rule's row fields, by key. Raises one ``ValueError`` naming every rule that cannot be."""
    wanted_schedules: dict[str, _ScheduleFields] = {}
    wanted_triggers: dict[str, _TriggerFields] = {}
    problems: list[str] = []

    def problem(label: str, error: ValueError) -> None:
        message = str(error)
        problems.append(message if message.startswith(label) else f"{label}: {message}")

    for schedule in document.schedules:
        label = f"Schedule {schedule.key!r}"
        try:
            wanted_schedules[schedule.key] = _ScheduleFields.of(schedule, _target(organization, label, schedule.agent, schedule.interface))
        except ValueError as error:
            problem(label, error)
    for trigger in document.triggers:
        label = f"Trigger {trigger.key!r}"
        try:
            wanted_triggers[trigger.key] = _TriggerFields.of(trigger, _target(organization, label, trigger.agent, trigger.interface))
        except ValueError as error:
            problem(label, error)
    if problems:
        raise ValueError("The wiregram cannot be imported. " + " | ".join(problems))
    return wanted_schedules, wanted_triggers


def _bring_in_line[Rule: models.Schedule | models.Trigger](model: type[Rule], wiregram: models.Wiregram, wanted: dict[str, _Fields[Rule]], enabled: dict[str, bool], caller: models.Caller) -> None:
    """Make the wiregram's rules of one kind exactly the document's: by key, in place."""
    model.objects.filter(wiregram=wiregram).exclude(wire_key__in=wanted).delete()
    for key, fields in wanted.items():
        rule = model.objects.filter(wiregram=wiregram, wire_key=key).first()
        if rule is None:
            fields.create(wiregram, key, caller, enabled[key])
        else:
            fields.update(rule)


def import_wiregram(document: WiregramModel, caller: models.Caller, principal: CallerContext | None = None) -> models.Wiregram:
    """Bring the organization's rules of this wiregram in line with ``document``; all or nothing."""
    organization = caller.organization
    wanted_schedules, wanted_triggers = _checked(document, organization)

    existing = models.Wiregram.objects.filter(organization=organization, key=document.key).first()
    owned = {schedule.wire_key: schedule for schedule in models.Schedule.objects.filter(wiregram=existing)} if existing is not None else {}
    # A schedule that goes, or whose run was planned on other terms, must not have that run handed
    # over. takt hears of all of it when the import commits, and of none of it if it fails.
    replanned = [key for key, schedule in owned.items() if key in wanted_schedules and wanted_schedules[key].retimes(schedule)]

    with transaction.atomic():
        for key, schedule in owned.items():
            if key not in wanted_schedules:
                schedules.drop_waiting_run(schedule, principal)
        wiregram, _ = models.Wiregram.objects.update_or_create(
            organization=organization,
            key=document.key,
            defaults=dict(caller=caller, name=document.name, description=document.description, document=document.model_dump(mode="json")),
        )
        _bring_in_line(models.Schedule, wiregram, dict(wanted_schedules), {rule.key: rule.enabled for rule in document.schedules}, caller)
        _bring_in_line(models.Trigger, wiregram, dict(wanted_triggers), {rule.key: rule.enabled for rule in document.triggers}, caller)

        for schedule in models.Schedule.objects.filter(wiregram=wiregram):
            schedules.plan(schedule, replan=schedule.wire_key in replanned, principal=principal)
    return wiregram


def delete_wiregram(wiregram: models.Wiregram, principal: CallerContext | None = None) -> None:
    """Remove a wiregram and the rules it owns. Waiting runs are cancelled; history is kept."""
    with transaction.atomic():
        for schedule in models.Schedule.objects.filter(wiregram=wiregram):
            schedules.drop_waiting_run(schedule, principal)
        wiregram.delete()


def _pinned_agent(rule: models.Schedule | models.Trigger, what: str) -> models.Agent:
    if rule.agent is None:
        raise ValueError(f"{what} {rule.name!r} is not pinned to an agent; a wiregram names its target as an agent and an interface")
    return rule.agent


def export_wiregram(key: str, name: str, description: str | None, listed_schedules: list[models.Schedule], listed_triggers: list[models.Trigger]) -> JSON:
    """Existing rules written down as a wiregram document, e.g. to import into another organization.

    A rule must be pinned to an agent: a wiregram names its target as agent + interface and has
    no way to say "any agent implementing this action".
    """
    document = WiregramModel(
        key=key,
        name=name,
        description=description,
        schedules=[
            WireScheduleModel(
                key=s.wire_key or f"schedule-{s.pk}",
                name=s.name,
                description=s.description,
                agent=_pinned_agent(s, "Schedule").name,
                interface=s.interface,
                args=s.args or {},
                enabled=s.enabled,
                ends_at=s.ends_at,
                max_runs=s.max_runs,
                interval_seconds=s.interval_seconds,
                cron=s.cron,
                timezone=s.timezone,
                ephemeral_runs=s.ephemeral_runs,
                overlap=s.overlap,
                catch_up=s.catch_up,
            )
            for s in listed_schedules
        ],
        triggers=[
            WireTriggerModel(
                key=t.wire_key or f"trigger-{t.pk}",
                name=t.name,
                description=t.description,
                agent=_pinned_agent(t, "Trigger").name,
                interface=t.interface,
                args=t.args or {},
                enabled=t.enabled,
                ends_at=t.ends_at,
                max_runs=t.max_runs,
                kind=t.kind,
                identifier=t.identifier,
                port=t.port,
                conditions=t.conditions or [],
                debounce_seconds=t.debounce_seconds,
            )
            for t in listed_triggers
        ],
    )
    return document.model_dump(mode="json")
