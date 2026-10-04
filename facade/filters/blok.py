# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for materialized bloks."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.order_type(models.MaterializedBlok)
class MaterializedBlokOrder:
    created_at: auto


@strawberry_django.filter_type(models.MaterializedBlok, description="A way to filter placements (space memberships)")
class MaterializedBlokFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by agent")
    def agent(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}agent_mappings__agent_id": value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: str | None, prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()
