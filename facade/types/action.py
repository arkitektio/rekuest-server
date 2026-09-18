"""The Action GraphQL type and its statistics resolver."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from kante.types import Info
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

from facade import enums, filters, models
from facade.type_gen import create_stats_type
from facade.types.base import build_prescoped_queryset, build_prescoper


@strawberry_django.type(models.Action, filters=filters.ActionFilter, pagination=True, ordering=filters.ActionOrder, description="Represents an executable action in the system.")
class Action:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the action.")
    hash: rscalars.ActionHash = strawberry_django.field(description="Unique hash identifying the action definition.")
    name: str = strawberry_django.field(description="Name of the action.")
    key: str = strawberry_django.field(description="Key of the action, used for grouping and identification.")
    app: App = strawberry_django.field(description="The app this action belongs to.")
    version: str = strawberry_django.field(description="Version string of the action.")
    kind: renums.ActionKind = strawberry_django.field(description="The kind or category of the action.")
    stateful: bool = strawberry_django.field(description="Indicates whether the action maintains state.")
    pure: bool = strawberry_django.field(description="Whether the action is pure: same args always produce the same result, no side effects — replayable.")
    idempotent: bool = strawberry_django.field(description="Whether the action is idempotent: safe to re-run with the same args — re-dispatchable on ambiguous executor loss.")
    allow_probe: bool = strawberry_django.field(description="Whether the action may be invoked as a probe (zero persistence, no history/replay/recovery). Declared by the action author.")
    description: str | None = strawberry_django.field(description="Optional description of the action.")
    collections: list["Collection"] = strawberry_django.field(description="Collections to which this action belongs.")
    implementations: list["Implementation"] = strawberry_django.field(description="List of implementations for this action.")
    scope: enums.ActionScope = strawberry_django.field(description="Scope of the action, e.g., user or system.")
    is_test_for: list["Action"] = strawberry_django.field(description="Actions for which this is a test.")
    is_dev: bool = strawberry_django.field(description="Marks whether the action is in development.")
    tests: list["Action"] = strawberry_django.field(description="List of tests associated with the action.")
    protocols: list["Protocol"] = strawberry_django.field(description="Protocols associated with the action.")
    defined_at: datetime.datetime = strawberry_django.field(description="Timestamp when the action was defined.")
    test_cases: list["TestCase"] | None = strawberry_django.field(description="Test cases for this action.")
    organization: "Organization" = strawberry_django.field(description="The organization that owns this action.")
    tasks: list["Task"] = strawberry_django.field(description="Tasks created for this action.")

    @strawberry_django.field(description="Get the latest completed task for this action.")
    def latest_task(self) -> Optional["Task"]:
        return models.Task.objects.filter(action=self, is_done=True).order_by("-created_at").first()

    @strawberry_django.field(description="Retrieve tasks where this action has run.")
    def runs(self) -> list["Task"] | None:
        return models.Task.objects.filter(action=self).order_by("-created_at")

    @strawberry_django.field(description="Input arguments (ports) for the action.")
    def args(self) -> list[rtypes.ArgPort]:
        return [rmodels.ArgPortModel(**i) for i in self.args]

    @strawberry_django.field(description="Output values (ports) returned by the action.")
    def returns(self) -> list[rtypes.ReturnPort]:
        return [rmodels.ReturnPortModel(**i) for i in self.returns]

    @strawberry_django.field(description="Port groups used in the action for organizing ports.")
    def port_groups(self) -> list[rtypes.PortGroup]:
        return [rmodels.PortGroupModel(**i) for i in self.port_groups]

    @strawberry_django.field(description="Check if the current user has pinned this action.")
    def pinned(self, info: Info) -> bool:
        user = info.context.request.user
        return self.pinned_by.filter(id=user.id).exists()

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="organization")


ActionStats, ActionStatsResolver = create_stats_type(
    models.Action,
    filters=filters.ActionFilter,
    # Action has no created_at; its creation timestamp is defined_at
    allowed_fields={
        "created_at": "defined_at",
    },
    allowed_datetime_fields={"created_at": "defined_at"},
    prescope=build_prescoper(field="organization"),
)
