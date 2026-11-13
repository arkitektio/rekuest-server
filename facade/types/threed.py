"""Spaces (rooms) and where agents stand in them."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, cast

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


@strawberry_django.type(
    models.Space,
    filters=filters.SpaceFilter,
    ordering=filters.SpaceOrder,
    pagination=True,
    description="A room: where the organization keeps its devices. What a 3D view shows is the action's `<Space room=...>` blok; where each device stands is kept here, the same for everyone.",
)
class Space:
    id: strawberry.ID
    name: str
    reference: str | None = strawberry_django.field(description="What finds the room again: a blok's `room`.")
    description: str | None
    creator: User
    created_at: datetime.datetime
    updated_at: datetime.datetime
    placements: list["Placement"] = strawberry_django.field(description="Where someone left each agent in this room.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Space], info: Info, **kwargs: object) -> QuerySet[models.Space]:
        return build_prescoped_queryset(info, queryset, field="organization")


def space_by_reference(info: Info, room: str) -> Space | None:
    """The caller's space that ``room`` names: null until someone kept a pose in it."""
    space = models.Space.objects.filter(organization=info.context.request.organization, reference=room.strip()).first()
    return cast("Space | None", space)


@strawberry_django.type(
    models.Placement,
    filters=filters.PlacementFilter,
    ordering=filters.PlacementOrder,
    pagination=True,
    description="Where someone left an agent in a space: its pose, the same for every viewer.",
)
class Placement:
    id: strawberry.ID
    space: Space
    agent: Agent
    role: str = strawberry_django.field(description="The dependency key the agent was placed under when its pose was kept.")
    affine_matrix: scalars.Args | None = strawberry_django.field(description="Where the agent stands: a 4x4 affine matrix, row-major, in meters with Y up.")
    updated_at: datetime.datetime

    @strawberry_django.field(description="Get the agent associated with this placement.")
    def name(self) -> str:
        return self.agent.name

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Placement], info: Info, **kwargs: object) -> QuerySet[models.Placement]:
        return build_prescoped_queryset(info, queryset, field="space__organization")
