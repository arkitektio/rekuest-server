"""3D models, spaces and placements."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from datalayer import types as dtypes
from facade import filters, models, scalars
from facade.types.base import build_prescoped_queryset
from rekuest_core import scalars as rscalars

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.auth import User
    from facade.types.blok import MaterializedBlok


@strawberry_django.type(
    models.ThreeDModel,
    filters=filters.ThreeDModelFilter,
    ordering=filters.ThreeDModelOrder,
    pagination=True,
    description="A 3D model file.",
)
class ThreeDModel:
    id: strawberry.ID
    name: str
    description: str | None
    transfer_function: str | None
    dependency: rscalars.AnyDefault | None = strawberry_django.field(description="The agent this model shows (an agent-dependency declaration), as stored.")
    file: dtypes.MediaStore
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.ThreeDModel], info: Info, **kwargs: object) -> QuerySet[models.ThreeDModel]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(
    models.Space,
    filters=filters.SpaceFilter,
    ordering=filters.SpaceOrder,
    pagination=True,
    description="A space where agents can interact.",
)
class Space:
    id: strawberry.ID
    name: str
    description: str | None
    creator: User
    created_at: datetime.datetime
    updated_at: datetime.datetime
    placements: list["Placement"]

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Space], info: Info, **kwargs: object) -> QuerySet[models.Space]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(
    models.Placement,
    filters=filters.PlacementFilter,
    ordering=filters.PlacementOrder,
    pagination=True,
    description="A placement of an agent in a space.",
)
class Placement:
    id: strawberry.ID
    space: Space
    agent: Agent
    blok: MaterializedBlok | None
    role: str
    affine_matrix: scalars.Args | None
    model: ThreeDModel | None

    @strawberry_django.field(description="Get the agent associated with this placement.")
    def name(self) -> str:
        return self.agent.name

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Placement], info: Info, **kwargs: object) -> QuerySet[models.Placement]:
        return build_prescoped_queryset(info, queryset, field="space__organization")
