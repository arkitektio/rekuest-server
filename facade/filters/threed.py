# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for 3D models, spaces and placements."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.order_type(models.ThreeDModel)
class ThreeDModelOrder:
    created_at: auto
    updated_at: auto
    name: auto


@strawberry_django.filter_type(models.ThreeDModel, description="A way to filter 3D models")
class ThreeDModelFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.ThreeDModel], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.ThreeDModel], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.ThreeDModel], value: str, prefix: str) -> tuple[QuerySet[models.ThreeDModel], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()


@strawberry_django.order_type(models.Space)
class SpaceOrder:
    created_at: auto
    updated_at: auto
    name: auto


@strawberry_django.filter_type(models.Space, description="A way to filter spaces")
class SpaceFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.Space], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Space], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.Space], value: str | None, prefix: str) -> tuple[QuerySet[models.Space], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()


@strawberry_django.order_type(models.Placement)
class PlacementOrder:
    role: auto
    created_at: auto


@strawberry_django.filter_type(models.Placement, description="A way to filter placements (space memberships)")
class PlacementFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.Placement], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Placement], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by space")
    def space(self, info: Info, queryset: QuerySet[models.Placement], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Placement], Q]:
        return queryset.filter(**{f"{prefix}space_id": value}), Q()

    @filter_field(description="Filter by agent")
    def agent(self, info: Info, queryset: QuerySet[models.Placement], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Placement], Q]:
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.Placement], value: str | None, prefix: str) -> tuple[QuerySet[models.Placement], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()
