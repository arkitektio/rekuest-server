"""Filters and orders for sessions."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from django.db.models.expressions import OrderBy
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.order_type(models.Session)
class SessionOrder:
    @strawberry_django.order_field(description="When the session started.")
    def started_at(self, info: Info, queryset: QuerySet[models.Session], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Session], list[OrderBy]]:
        if not value:
            return queryset, []
        return queryset, [value.resolve(f"{prefix}created_at")]


@strawberry_django.filter_type(models.Session, description="A way to filter sessions")
class SessionFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.Session], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Session], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by space")
    def agent(self, info: Info, queryset: QuerySet[models.Session], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Session], Q]:
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()
