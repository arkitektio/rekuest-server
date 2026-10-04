# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for protocols, toolboxes and shortcuts."""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from facade import inputs, managers, models


@strawberry_django.order_type(models.Protocol)
class ProtocolOrder:
    name: auto


@strawberry_django.order_type(models.Shortcut)
class ShortcutOrder:
    name: auto


@strawberry_django.order_type(models.Toolbox)
class ToolboxOrder:
    name: auto


@strawberry_django.filter_type(models.Protocol)
class ProtocolFilter:
    name: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Protocol], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Protocol], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.Protocol], value: str, prefix: str) -> tuple[QuerySet[models.Protocol], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()


@strawberry_django.filter_type(models.Toolbox)
class ToolboxFilter:
    name: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Toolbox], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Toolbox], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.Toolbox], value: str, prefix: str) -> tuple[QuerySet[models.Toolbox], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()


@strawberry_django.filter_type(models.Shortcut)
class ShortcutFilter:
    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.Shortcut], value: str, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Shortcut], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def demands(self, info: Info, queryset: QuerySet[models.Shortcut], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        if len(value) == 0:
            return queryset, Q()

        # Shortcuts have no relational port rows; the manager falls back to the JSONB scan.
        ids = managers.get_action_ids_by_port_demands([managers.PortDemand.of(demand) for demand in value], model="facade_shortcut")
        return queryset.filter(**{f"{prefix}id__in": ids}), Q()

    @filter_field
    def toolbox(self, info: Info, queryset: QuerySet[models.Shortcut], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}toolbox_id": value}), Q()
