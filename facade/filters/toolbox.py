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
from facade.filters.action import any_port_demands, assignable_first


def caller_app(info: Info) -> str | None:
    """The identifier of the app the caller signed in with (its client's release), if it has one."""
    client = getattr(info.context.request, "client", None)
    release = client.release if client is not None else None
    return release.app.identifier if release is not None else None


def _anyone_or(field: str, value: object) -> Q:
    """A binding's ``field`` left null (anyone), or set to the caller's ``value``; only null when the caller has none."""
    anyone = Q(**{f"{field}__isnull": True})
    return anyone | Q(**{field: value}) if value is not None else anyone


def pinned_for_caller(info: Info) -> Exists:
    """Whether a binding of the outer shortcut matches the caller: every one of its user, app,
    client and device that is set is the caller's."""
    request = info.context.request
    client = getattr(request, "client", None)
    device = client.device if client is not None else None
    matches = (
        _anyone_or("user", request.user)
        & _anyone_or("app", caller_app(info))
        & _anyone_or("client", client.client_id if client is not None else None)
        & _anyone_or("device", device.device_id if device is not None else None)
    )
    return Exists(models.ShortcutBinding.objects.filter(matches, shortcut=OuterRef("pk")))


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

    @strawberry_django.order_field(description="Pinned for the caller (an admin bound it to them, or to where they call from) first (DESC) or last (ASC). Put it before `assignable`, which adds the tie-break.")
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

        # Shortcuts have no relational port rows: the JSONB scan, nested as a subquery over the
        # caller's organization and returned as the Q so OR/NOT compose (see ActionFilter.demands).
        subquery = managers.get_port_demand_subquery([managers.PortDemand.of(demand) for demand in value], organization_id=info.context.request.organization.id, owner="shortcut")
        return queryset, Q(**{f"{prefix}id__in": subquery})

    @filter_field(description="Any of these demand sets: the rows that satisfy every demand of at least one set (each set as `demands`). One statement, so prefer it to `OR`-ing `demands`, which tests every row of the organization. An empty list is ignored; an empty set is an error.")
    def any_demands(self, info: Info, queryset: QuerySet[models.Shortcut], value: list[list[inputs.PortDemandInput]], prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset, any_port_demands(info, value, "shortcut", f"{prefix}id")

    @filter_field(description="Whether an agent that can take the shortcut's action is connected now (`Action.assignable`).")
    def assignable(self, info: Info, queryset: QuerySet[models.Shortcut], value: bool, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}action__assignable": value}), Q()

    @filter_field
    def toolbox(self, info: Info, queryset: QuerySet[models.Shortcut], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        return queryset.filter(**{f"{prefix}toolbox_id": value}), Q()

    @filter_field(description="Whether an admin pinned the shortcut for the caller: a binding whose user, app, client and device are each the caller's or left open.")
    def pinned(self, info: Info, queryset: QuerySet[models.Shortcut], value: bool, prefix: str) -> tuple[QuerySet[models.Shortcut], Q]:
        if prefix:
            raise ValueError("`pinned` filters shortcuts themselves, not a relation.")
        bound = pinned_for_caller(info)
        return queryset.filter(bound if value else ~bound), Q()
