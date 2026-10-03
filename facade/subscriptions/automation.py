"""Change feeds for automation: the organization's signals, and its schedules and triggers.

Slim snapshots, like the other feeds: enough to update a list in place, with the id to fetch
the rest. Signals are written by takt, which publishes; rules are written here (a user's edit)
and by takt (its bookkeeping: failures, counts), and both publish on the same channel.
"""

import datetime
from typing import AsyncGenerator

import strawberry
from kante.types import Info

from facade import enums, models
from facade.channels import rule_channel, signal_channel


@strawberry.type(description="Slim snapshot of a signal for the change feed.")
class SignalChange:
    id: strawberry.ID
    service: str
    kind: enums.SignalKind
    identifier: str
    object: str
    received_at: datetime.datetime
    processed_at: datetime.datetime | None = strawberry.field(description="When triggers were matched against it; null = not yet.")
    fired: int = strawberry.field(description="How many triggers fired a run for it so far.")

    @classmethod
    async def load(cls, pk: int) -> "SignalChange":
        signal = await models.Signal.objects.aget(pk=pk)
        fired = await models.Firing.objects.filter(signal=signal, outcome=enums.FiringOutcomeChoices.FIRED).acount()
        return cls(
            id=strawberry.ID(str(signal.pk)),
            service=signal.service,
            kind=enums.SignalKind(signal.kind),
            identifier=signal.identifier,
            object=signal.object,
            received_at=signal.received_at,
            processed_at=signal.processed_at,
            fired=fired,
        )


@strawberry.type(description="A signal arrived (create), or triggers were matched against it (update).")
class SignalChangeEvent:
    create: SignalChange | None = None
    update: SignalChange | None = None


@strawberry.type(description="Slim snapshot of a schedule or trigger for the change feeds.")
class RuleChange:
    id: strawberry.ID
    name: str
    enabled: bool
    consecutive_failures: int
    last_error: str | None
    run_count: int
    last_fired_at: datetime.datetime | None
    updated_at: datetime.datetime

    @classmethod
    def from_model(cls, rule: "models.Schedule | models.Trigger") -> "RuleChange":
        return cls(
            id=strawberry.ID(str(rule.pk)),
            name=rule.name,
            enabled=rule.enabled,
            consecutive_failures=rule.consecutive_failures,
            last_error=rule.last_error,
            run_count=rule.run_count,
            last_fired_at=rule.last_fired_at,
            updated_at=rule.updated_at,
        )


@strawberry.type(description="A schedule or trigger was created, changed (by a user, or by its runs) or deleted.")
class RuleChangeEvent:
    create: RuleChange | None = None
    update: RuleChange | None = None
    delete: strawberry.ID | None = None


async def signals(self, info: Info) -> AsyncGenerator[SignalChangeEvent, None]:
    """Subscribe to the signals services send about the organization's objects."""
    organization = info.context.request.organization
    async for message in signal_channel.listen(info.context, [f"signals_org_{organization.id}"]):
        try:
            if message.create is not None:
                yield SignalChangeEvent(create=await SignalChange.load(message.create))
            elif message.update is not None:
                yield SignalChangeEvent(update=await SignalChange.load(message.update))
        except models.Signal.DoesNotExist:
            continue  # gone again (retention) before this subscriber got to it


async def _rules(info: Info, model, which: str) -> AsyncGenerator[RuleChangeEvent, None]:
    organization = info.context.request.organization
    async for message in rule_channel.listen(info.context, [f"rules_org_{organization.id}"]):
        pk = getattr(message, which)
        if pk is None:
            continue
        if message.change == "delete":
            yield RuleChangeEvent(delete=strawberry.ID(str(pk)))
            continue
        try:
            snapshot = RuleChange.from_model(await model.objects.aget(pk=pk))
        except model.DoesNotExist:
            continue
        yield RuleChangeEvent(create=snapshot) if message.change == "create" else RuleChangeEvent(update=snapshot)


async def schedules(self, info: Info) -> AsyncGenerator[RuleChangeEvent, None]:
    """Subscribe to the organization's schedules being created, changed or deleted."""
    async for event in _rules(info, models.Schedule, "schedule"):
        yield event


async def triggers(self, info: Info) -> AsyncGenerator[RuleChangeEvent, None]:
    """Subscribe to the organization's triggers being created, changed or deleted."""
    async for event in _rules(info, models.Trigger, "trigger"):
        yield event
