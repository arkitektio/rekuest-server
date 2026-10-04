# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for actions."""

from __future__ import annotations

import datetime
from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Max, Q, QuerySet
from django.db.models.expressions import OrderBy
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from embeddings.search import hybrid_search
from facade import inputs, managers, models
from rekuest_core import enums as renums


def _filter_by_port_demands(info: Info, queryset: QuerySet[models.Action], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Action], Q]:
    """Shared body of ``demands`` and ``object_demands`` — one port-demand resolution path.

    The two filter fields are the same computation; whether a demand is "structural" or
    "object" is decided by the caller populating ``PortMatchInput.descriptors``, not by
    which entry point they picked.
    """
    if len(value) == 0:
        return queryset, Q()

    # RawSQL subquery: the matching statement runs nested inside this query — one round
    # trip, no id materialization in Python.
    subquery = managers.get_action_port_demand_subquery([managers.PortDemand.of(demand) for demand in value], organization_id=info.context.request.organization.id)
    return queryset.filter(**{f"{prefix}id__in": subquery}), Q()


@strawberry_django.order_type(models.Action)
class ActionOrder:
    defined_at: auto

    @strawberry_django.order_field
    def used_at(self, info: Info, queryset: QuerySet[models.Action], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Action], list[OrderBy]]:
        if not value:
            return queryset, []
        queryset = queryset.annotate(latest_task_time=Max(f"{prefix}task__created_at"))
        return queryset, [value.resolve("latest_task_time")]


@strawberry_django.filter_type(models.Action)
class ActionFilter:
    name: Optional[FilterLookup[str]]

    @filter_field(description="Search by name: a case-insensitive substring, or semantic similarity of the query to the action's name and description. Substring matches rank first, then by similarity; an explicit `ordering` replaces that ranking.")
    def search(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return hybrid_search(queryset, prefix, value, Q(**{f"{prefix}name__icontains": value}))

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Action], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def demands(self, info: Info, queryset: QuerySet[models.Action], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return _filter_by_port_demands(info, queryset, value, prefix)

    @filter_field
    def object_demands(self, info: Info, queryset: QuerySet[models.Action], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        """Filter to actions whose ports accept the given concrete runtime objects.

        Same input shape as ``demands``; kept as a separate entry point for the "what accepts
        this concrete object" use case, where each match carries the object's runtime
        ``descriptors``, evaluated against the port's compiled ``requires`` micro-constraint —
        so this keeps only actions a real object can actually be passed to, not merely
        structurally-compatible ones.
        """
        return _filter_by_port_demands(info, queryset, value, prefix)

    @filter_field
    def protocols(self, info: Info, queryset: QuerySet[models.Action], value: list[str], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}protocols__name__in": value}), Q()

    @filter_field
    def kind(self, info: Info, queryset: QuerySet[models.Action], value: renums.ActionKind, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}kind": value}), Q()

    @filter_field
    def in_collection(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}collections__name": value}), Q()

    @filter_field
    def used_before(self, info: Info, queryset: QuerySet[models.Action], value: datetime.datetime, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}tasks__created_at__lt": value}), Q()

    @filter_field
    def used_after(self, info: Info, queryset: QuerySet[models.Action], value: datetime.datetime, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}tasks__created_at__gt": value}), Q()

    @filter_field
    def stateful(self, info: Info, queryset: QuerySet[models.Action], value: bool, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}stateful": value}), Q()

    @filter_field(description="Filter using app identifier")
    def app_identifier(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}app__identifier": value}), Q()
