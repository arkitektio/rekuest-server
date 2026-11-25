# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for agent states."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.filter_type(models.State, description="A way to filter states")
class StateFilter:
    @filter_field(description="Filter by IDs")
    def ids(
        self,
        info: Info,
        queryset: QuerySet[models.State],
        value: list[strawberry.ID],
        prefix: str,
    ) -> tuple[QuerySet[models.State], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Only the states of this agent.")
    def agent(
        self,
        info: Info,
        queryset: QuerySet[models.State],
        value: strawberry.ID,
        prefix: str,
    ) -> tuple[QuerySet[models.State], Q]:
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Only the states of the app with this identifier.")
    def app_identifier(
        self,
        info: Info,
        queryset: QuerySet[models.State],
        value: str,
        prefix: str,
    ) -> tuple[QuerySet[models.State], Q]:
        return queryset.filter(**{f"{prefix}app_identifier": value}), Q()

    @filter_field(
        description="Search by interface, key or the agent's name (case-insensitive substring)."
    )
    def search(
        self,
        info: Info,
        queryset: QuerySet[models.State],
        value: str | None,
        prefix: str,
    ) -> tuple[QuerySet[models.State], Q]:
        if not value:
            return queryset, Q()
        return queryset.filter(
            Q(**{f"{prefix}interface__icontains": value})
            | Q(**{f"{prefix}key__icontains": value})
            | Q(**{f"{prefix}agent__name__icontains": value})
        ), Q()


@strawberry_django.order_type(models.State)
class StateOrder:
    created_at: auto
    updated_at: auto
