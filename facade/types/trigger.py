"""The Trigger and Signal types: users' rules over what services announced, and the announcements."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from kante.types import Info
from rekuest_core import scalars as rscalars

from facade import enums, filters, models, rules
from facade.types.base import build_prescoped_queryset


@strawberry_django.type(models.Signal, filters=filters.SignalFilter, ordering=filters.SignalOrder, pagination=True, description="Something a service announced: an object of a structure was created, updated or deleted.")
class Signal:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the signal.")
    service_name: str = strawberry_django.field(field_name="service", description="The name of the service that sent it.")

    @strawberry_django.field(description="The service that sent it, as the hub catalogues it; null when it is no longer catalogued.")
    def service(self) -> Optional["Service"]:
        return models.Service.objects.filter(name=self.service).first()

    kind: enums.SignalKind = strawberry_django.field(description="What happened to the object.")
    identifier: str = strawberry_django.field(description="The object's structure identifier, e.g. @mikro/arraydataset.")
    object: str = strawberry_django.field(description="The object's id within its structure.")
    descriptors: rscalars.AnyDefault = strawberry_django.field(description="The object's descriptors (flat key → value).")
    causing_task: Optional["Task"] = strawberry_django.field(description="The task the object was created in, verified from its provenance token.")
    occurred_at: datetime.datetime | None = strawberry_django.field(description="When it happened, per the service.")
    received_at: datetime.datetime = strawberry_django.field(description="When rekuest received it.")
    processed_at: datetime.datetime | None = strawberry_django.field(description="When triggers were matched against it.")

    @strawberry_django.field(description="The runs this signal fired.")
    def runs(self) -> list["Task"]:
        return list(self.tasks.order_by("created_at"))

    @strawberry_django.field(description="What became of every trigger that listened for it. Empty once processed: nobody listened.")
    def firings(self) -> list["Firing"]:
        return list(self.firings.select_related("trigger").order_by("created_at"))

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.Trigger, filters=filters.TriggerFilter, ordering=filters.TriggerOrder, pagination=True, description="A rule over signals: on a signal of this kind and structure whose descriptors match, run the action with the object in `port`.")
class Trigger:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the trigger.")
    name: str = strawberry_django.field(description="Human-readable name.")
    enabled: bool = strawberry_django.field(description="A disabled trigger fires nothing.")
    kind: enums.SignalKind = strawberry_django.field(description="The signal kind it reacts to.")
    identifier: str = strawberry_django.field(description="The structure identifier it reacts to.")
    conditions: rscalars.AnyDefault = strawberry_django.field(description="Extra descriptor conditions (key, operator, value), requires-style.")
    action: "Action" = strawberry_django.field(description="The action every run assigns.")
    agent: Optional["Agent"] = strawberry_django.field(description="The agent runs are pinned to, if any.")
    interface: str | None = strawberry_django.field(description="The implementation interface on the pinned agent.")
    port: str = strawberry_django.field(description="The STRUCTURE argument that receives the signalled object.")
    args: rscalars.AnyDefault = strawberry_django.field(description="The other args of every run.")
    created_at: datetime.datetime = strawberry_django.field(description="Creation timestamp.")
    updated_at: datetime.datetime = strawberry_django.field(description="Last update timestamp.")
    consecutive_failures: int = strawberry_django.field(description="Firings in a row that could not create a run.")
    last_error: str | None = strawberry_django.field(description="Why the last firing did not create a run.")
    caller: "Caller" = strawberry_django.field(description="The owner; runs are assigned as this identity.")
    description: str | None = strawberry_django.field(description="What the trigger is for.")
    ends_at: datetime.datetime | None = strawberry_django.field(description="Nothing fires after this moment.")
    max_runs: int | None = strawberry_django.field(description="Nothing fires once it created this many runs.")
    run_count: int = strawberry_django.field(description="Runs it created so far.")
    last_fired_at: datetime.datetime | None = strawberry_django.field(description="When it last created a run.")
    last_error_at: datetime.datetime | None = strawberry_django.field(description="When `lastError` was written.")
    debounce_seconds: int | None = strawberry_django.field(description="Fires at most once per object within this many seconds.")
    wiregram: Optional["Wiregram"] = strawberry_django.field(description="The wiregram that owns this trigger, if it was imported with one.")
    wire_key: str | None = strawberry_django.field(description="What the wiregram's document calls this trigger.")

    @strawberry_django.field(description="Whether it stopped by itself: its end passed, or it created its last allowed run.")
    def exhausted(self) -> bool:
        return rules.exhausted(self)

    @strawberry_django.field(description="What became of it for the most recent signals it listened for, newest first.")
    def firings(self, limit: int = 20) -> list["Firing"]:
        return list(self.firings.select_related("signal").order_by("-created_at")[: max(0, min(limit, 200))])

    @strawberry_django.field(description="The most recent runs, newest first.")
    def runs(self, limit: int = 20) -> list["Task"]:
        return list(self.tasks.order_by("-created_at")[: max(0, min(limit, 200))])

    @strawberry_django.field(description="When its newest run was created; null when it never fired (or its runs were since deleted by retention).")
    def last_run_at(self) -> datetime.datetime | None:
        return self.tasks.order_by("-created_at").values_list("created_at", flat=True).first()

    @strawberry_django.field(description="A dry run: the stored signals this trigger would fire on as it is now, newest first. Applies its conditions and its port's requires, like a real firing; fires nothing.")
    def matching_signals(self, limit: int = 20) -> list["Signal"]:
        from facade import triggers

        port = models.ArgPort.objects.filter(action_id=self.action_id, parent__isnull=True, key=self.port).first()
        paths = [self.compiled_jsonpath, port.compiled_jsonpath if port is not None else None]
        return list(triggers.matching_signals(self.caller.organization_id, self.kind, self.identifier, paths, limit))

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="caller__organization")


@strawberry_django.type(
    models.Firing,
    filters=filters.FiringFilter,
    ordering=filters.FiringOrder,
    pagination=True,
    description="What became of one trigger for one signal: it fired a run, was rejected, or failed. Kept as long as the signal.",
)
class Firing:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the firing.")
    signal: "Signal" = strawberry_django.field(description="The signal.")
    trigger: "Trigger" = strawberry_django.field(description="The trigger that was tried on it.")
    outcome: enums.FiringOutcome = strawberry_django.field(description="What became of it.")
    reason: str | None = strawberry_django.field(description="Why it was rejected or failed; for a replay, that it was one.")
    task: Optional["Task"] = strawberry_django.field(description="The run it created, while that run exists.")
    replay: bool = strawberry_django.field(description="Fired by hand on a stored signal, not by the signal arriving.")
    created_at: datetime.datetime = strawberry_django.field(description="When the trigger was tried.")

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="signal__organization")


@strawberry_django.type(models.SignalDeclaration, description="A signal a service of this hub declares it emits (from its manifest). Hub-wide.")
class SignalDeclaration:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the declaration.")
    identifier: str = strawberry_django.field(description="The structure identifier of the objects signalled.")
    kind: enums.SignalKind = strawberry_django.field(description="What happens to them.")
    descriptor_keys: list[str] = strawberry_django.field(description="The descriptor keys each signal carries — what trigger conditions may test.")
    description: str | None = strawberry_django.field(description="What the service says about the signal.")

    service: "Service" = strawberry_django.field(description="The service that emits it.")

    @strawberry_django.field(description="Your organization's triggers that wait for this signal.")
    def triggers(self, info: Info) -> list["Trigger"]:
        return list(models.Trigger.objects.filter(kind=self.kind, identifier=self.identifier, caller__organization=info.context.request.organization).order_by("name"))
