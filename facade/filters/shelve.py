# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for memory/filesystem shelves and drawers."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.filter_type(models.MemoryShelve, description="A way to filter shelved items")
class MemoryShelveFilter:
    agent: strawberry.ID | None

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.MemoryShelve], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.MemoryShelve], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.order_type(models.MemoryShelve)
class MemoryShelveOrder:
    name: auto


@strawberry_django.filter_type(models.MemoryDrawer, description="A way to filter shelved items")
class MemoryDrawerFilter:
    shelve: strawberry.ID | None
    agent: strawberry.ID | None

    @filter_field
    def implementation(self, info: Info, queryset: QuerySet[models.MemoryDrawer], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.MemoryDrawer], Q]:
        return queryset.filter(**{f"{prefix}shelve__agent__implementations": value}), Q()

    @filter_field
    def identifier(self, info: Info, queryset: QuerySet[models.MemoryDrawer], value: str, prefix: str) -> tuple[QuerySet[models.MemoryDrawer], Q]:
        return queryset.filter(**{f"{prefix}identifier": value}), Q()

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.MemoryDrawer], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.MemoryDrawer], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.MemoryDrawer], value: str, prefix: str) -> tuple[QuerySet[models.MemoryDrawer], Q]:
        return queryset.filter(**{f"{prefix}label__icontains": value}), Q()
