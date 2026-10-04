# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for dashboard placements."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.filter_type(models.DashboardPlacement, description="A way to filter placements (space memberships)")
class DashboardPlacementFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.DashboardPlacement], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.DashboardPlacement], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by space")
    def space(self, info: Info, queryset: QuerySet[models.DashboardPlacement], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.DashboardPlacement], Q]:
        return queryset.filter(**{f"{prefix}space_id": value}), Q()

    @filter_field(description="Filter by agent")
    def agent(self, info: Info, queryset: QuerySet[models.DashboardPlacement], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.DashboardPlacement], Q]:
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.DashboardPlacement], value: str | None, prefix: str) -> tuple[QuerySet[models.DashboardPlacement], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()


@strawberry_django.order_type(models.DashboardPlacement)
class DashboardPlacementOrder:
    role: auto
    created_at: auto
