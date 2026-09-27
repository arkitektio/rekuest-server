"""The Trigger and Signal types: users' rules over what services announced, and the announcements."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from rekuest_core import scalars as rscalars

from facade import enums, models
from facade.types.base import build_prescoped_queryset


@strawberry_django.type(models.Signal, pagination=True, description="Something a service announced: an object of a structure was created, updated or deleted.")
class Signal:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the signal.")
    service: str = strawberry_django.field(description="The service that sent it.")
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

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.Trigger, pagination=True, description="A rule over signals: on a signal of this kind and structure whose descriptors match, run the action with the object in `port`.")
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

    @strawberry_django.field(description="The most recent runs, newest first.")
    def runs(self, limit: int = 20) -> list["Task"]:
        return list(self.tasks.order_by("-created_at")[: max(0, min(limit, 200))])

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="caller__organization")


@strawberry_django.type(models.SignalDeclaration, description="A signal a service of this hub declares it emits (from its manifest). Hub-wide.")
class SignalDeclaration:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the declaration.")
    identifier: str = strawberry_django.field(description="The structure identifier of the objects signalled.")
    kind: enums.SignalKind = strawberry_django.field(description="What happens to them.")
    descriptor_keys: list[str] = strawberry_django.field(description="The descriptor keys each signal carries — what trigger conditions may test.")
    description: str | None = strawberry_django.field(description="What the service says about the signal.")

    @strawberry_django.field(description="The service that emits it.")
    def service(self) -> str:
        return self.agent.name
