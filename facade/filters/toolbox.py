# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for protocols, toolboxes and shortcuts."""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.db.models.expressions import OrderBy
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from facade import inputs, managers, models
from facade.filters.action import assignable_first


def caller_app(info: Info) -> str | None:
    """The identifier of the app the caller signed in with (its client's release), if it has one."""
    client = getattr(info.context.request, "client", None)
    release = client.release if client is not None else None
    return release.app.identifier if release is not None else None


def pinned_for_caller(info: Info) -> Exists:
    """Whether the outer shortcut is bound to the caller: in the app they signed in with, or in every app."""
    app = caller_app(info)
    in_app = Q(app__isnull=True) | Q(app=app) if app else Q(app__isnull=True)
    bindings = models.ShortcutBinding.objects.filter(in_app, shortcut=OuterRef("pk"), user=info.context.request.user)
    return Exists(bindings)


@strawberry_django.order_type(models.Protocol)
class ProtocolOrder:
    name: auto


@strawberry_django.order_type(models.Shortcut)
class ShortcutOrder:
    name: auto

    @strawberry_django.order_field(description="Runnable first (DESC) or last (ASC) by the shortcut's `Action.assignable`; the order the filters set is the tie-break.")
    def assignable(self, info: Info, queryset: QuerySet[models.Shortcut], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Shortcut], list[OrderBy | str]]:
        if not value:
            return queryset, []
        return queryset, assignable_first(queryset, value, f"{prefix}action__assignable")

    @strawberry_django.order_field(description="Pinned for the caller (an admin bound it to them in this app) first (DESC) or last (ASC). Put it before `assignable`, which adds the tie-break.")
    def pinned(self, info: Info, queryset: QuerySet[models.Shortcut], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Shortcut], list[OrderBy | str]]:
        if not value or prefix:
            return queryset, []
        return queryset.alias(pinned_order=pinned_for_caller(info)), [value.resolve("pinned_order")]


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

    @filter_field(description="Whether an agent that can take the shortcut's action is connected now (`Action.assignable`).")
    def assignable(self, info: Info, queryset: QuerySet[models.Shortcut], value: bool, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}action__assignable": value}), Q()

    @filter_field
    def toolbox(self, info: Info, queryset: QuerySet[models.Shortcut], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}toolbox_id": value}), Q()

    @filter_field(description="Whether an admin bound the shortcut to the caller, in the app they signed in with or in every app.")
    def pinned(self, info: Info, queryset: QuerySet[models.Shortcut], value: bool, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        if prefix:
            raise ValueError("`pinned` filters shortcuts themselves, not a relation.")
        bound = pinned_for_caller(info)
        return queryset.filter(bound if value else ~bound), Q()
