"""Spaces, the agents placed in them and the 3D bloks placed in them."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models, scalars
from facade.types.base import build_prescoped_queryset

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.auth import User
    from facade.types.blok import MaterializedBlok


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
    blok_placements: list["SpacePlacement"] = strawberry_django.field(description="The 3D bloks placed in this space, each at its affine transform.")

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

    @strawberry_django.field(description="Get the agent associated with this placement.")
    def name(self) -> str:
        return self.agent.name

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Placement], info: Info, **kwargs: object) -> QuerySet[models.Placement]:
        return build_prescoped_queryset(info, queryset, field="space__organization")


@strawberry_django.type(
    models.SpacePlacement,
    filters=filters.SpacePlacementFilter,
    ordering=filters.SpacePlacementOrder,
    pagination=True,
    description="A 3D blok placed in a space at an affine transform: a Model or Robot blok over a kulisse asset, with no agent of its own.",
)
class SpacePlacement:
    id: strawberry.ID
    space: Space
    blok: MaterializedBlok
    name: str | None
    affine_matrix: scalars.Args | None = strawberry_django.field(description="Where the blok sits: a 4x4 affine matrix, row-major, from the blok's frame into the space's. Null is the identity.")
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.SpacePlacement], info: Info, **kwargs: object) -> QuerySet[models.SpacePlacement]:
        return build_prescoped_queryset(info, queryset, field="space__organization")
