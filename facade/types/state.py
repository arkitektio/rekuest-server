"""State definitions, states, patches and snapshots."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import enums, models, scalars
from facade.types.base import build_prescoped_queryset, row_of
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.task import Task


@strawberry_django.type(models.StateDefinition)
class StateDefinition:
    id: strawberry.ID
    hash: str
    name: str

    @strawberry_django.field()
    def ports(self) -> list[rtypes.ReturnPort]:
        return [rtypes.ReturnPort.from_pydantic(rmodels.ReturnPortModel(**i)) for i in self.ports]

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.StateDefinition], info: Info, **kwargs: object) -> QuerySet[models.StateDefinition]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.State)
class State:
    id: strawberry.ID
    definition: StateDefinition = strawberry_django.field(description="The schema definition for this state.")
    agent: Agent = strawberry_django.field(description="The agent to which this state belongs.")
    interface: str = strawberry_django.field(description="The interface this state is associated with.")
    key: str | None = strawberry_django.field(description="The stable identity key of this state, matched by state demands (defaults to the interface at registration).")
    app_identifier: str | None = strawberry_django.field(description="The identifier of the app providing this state (defaults to the owning agent's app identifier).")
    created_at: datetime.datetime = strawberry_django.field(description="Timestamp when this state was created.")
    updated_at: datetime.datetime = strawberry_django.field(description="Timestamp when this state was last updated.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.State], info: Info, **kwargs: object) -> QuerySet[models.State]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")


@strawberry.type
class JSONPatch:
    op: enums.JSONPatchOperation
    path: str
    value: scalars.Args


@strawberry_django.type(models.Patch)
class Patch:
    id: strawberry.ID
    op: str
    path: str
    value: scalars.Args
    old_value: scalars.Args | None = strawberry_django.field(description="The value the patch replaced, when the agent reported it.")
    timestamp: datetime.datetime

    # Patch/Snapshot store exactly one revision column, ``global_rev``, the revision *after* the
    # patch applies (see facade.logic.get_latest_state). These were declared as real columns that
    # never existed, so every query touching them raised FieldError; they are derived now.
    @strawberry.field(description="Global revision this patch applied to (global_rev - 1).")
    def global_current_revision(self) -> int:
        row = row_of(self, models.Patch)
        return row.global_rev - 1

    @strawberry.field(description="Global revision produced by this patch (global_rev).")
    def global_future_revision(self) -> int:
        row = row_of(self, models.Patch)
        return row.global_rev

    @strawberry.field(description="The session identifier string this row belongs to.")
    def session_id(self) -> str | None:
        row = row_of(self, models.Patch)
        return row.session.session_id if row.session_id else None

    task: Task | None
    state: State
    interface: str

    @strawberry.field
    def patch(self) -> JSONPatch:
        return JSONPatch(op=enums.JSONPatchOperation(self.op), path=self.path, value=self.value)

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Patch], info: Info, **kwargs: object) -> QuerySet[models.Patch]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")


@strawberry_django.type(models.Snapshot)
class Snapshot:
    id: strawberry.ID
    value: scalars.Args
    timestamp: datetime.datetime

    @strawberry.field(description="Global revision this snapshot represents (global_rev).")
    def global_revision(self) -> int:
        row = row_of(self, models.Snapshot)
        return row.global_rev

    @strawberry.field(description="The session identifier string this row belongs to.")
    def session_id(self) -> str | None:
        row = row_of(self, models.Snapshot)
        return row.session.session_id if row.session_id else None

    state: State

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Snapshot], info: Info, **kwargs: object) -> QuerySet[models.Snapshot]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")
