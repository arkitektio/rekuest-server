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


def assignable_first(queryset: QuerySet[models.Action] | QuerySet[models.Shortcut], value: strawberry_django.Ordering, field: str) -> list[OrderBy | str]:
    """``field`` first, then the order the queryset already has — a search's ranking, which
    strawberry_django would otherwise replace — then ``pk``, so an offset window is stable."""
    existing = list(queryset.query.order_by) or list(queryset.model._meta.ordering)
    tie_break = [*existing, *([] if "pk" in existing else ["pk"])]
    return [value.resolve(field), *tie_break]


def _filter_by_port_demands(info: Info, queryset: QuerySet[models.Action], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Action], Q]:
    """Shared body of ``demands`` and ``object_demands`` — one port-demand resolution path.

    The two filter fields are the same computation; whether a demand is "structural" or
    "object" is decided by the caller populating ``PortMatchInput.descriptors``, not by
    which entry point they picked.
    """
    if len(value) == 0:
        return queryset, Q()

    # RawSQL subquery: the matching statement runs nested inside this query — one round
    # trip, no id materialization in Python. Returned as the Q, not applied, so ``OR`` and
    # ``NOT`` compose with it (strawberry_django applies a field's own ``.filter()`` always).
    subquery = managers.get_port_demand_subquery([managers.PortDemand.of(demand) for demand in value], organization_id=info.context.request.organization.id)
    return queryset, Q(**{f"{prefix}id__in": subquery})


def any_port_demands(info: Info, value: list[list[inputs.PortDemandInput]], owner: managers.DemandOwner, column: str) -> Q:
    """Shared body of every ``any_demands`` field: ``column`` in the ``owner`` rows meeting at least one set.

    An empty list is ignored, like an empty ``demands``.
    """
    if len(value) == 0:
        return Q()
    sets = [[managers.PortDemand.of(demand) for demand in demands] for demands in value]
    return Q(**{f"{column}__in": managers.get_any_port_demand_subquery(sets, organization_id=info.context.request.organization.id, owner=owner)})


@strawberry_django.order_type(models.Action)
class ActionOrder:
    defined_at: auto

    @strawberry_django.order_field
    def used_at(self, info: Info, queryset: QuerySet[models.Action], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Action], list[OrderBy]]:
        if not value:
            return queryset, []
        queryset = queryset.annotate(latest_task_time=Max(f"{prefix}task__created_at"))
        return queryset, [value.resolve("latest_task_time")]

    @strawberry_django.order_field(description="Runnable first (DESC) or last (ASC) by `Action.assignable`. The order the filters set (a `search`'s ranking) is kept as the tie-break, so `search` with this ordering lists runnable matches first, each group still ranked by relevance.")
    def assignable(self, info: Info, queryset: QuerySet[models.Action], value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet[models.Action], list[OrderBy | str]]:
        if not value:
            return queryset, []
        return queryset, assignable_first(queryset, value, f"{prefix}assignable")


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

    @filter_field(description="Any of these demand sets: the rows that satisfy every demand of at least one set (each set as `demands`). One statement, so prefer it to `OR`-ing `demands`, which tests every row of the organization. An empty list is ignored; an empty set is an error.")
    def any_demands(self, info: Info, queryset: QuerySet[models.Action], value: list[list[inputs.PortDemandInput]], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset, any_port_demands(info, value, "action", f"{prefix}id")

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

    @filter_field(description="Whether an agent that can take the action is connected now (`Action.assignable`).")
    def assignable(self, info: Info, queryset: QuerySet[models.Action], value: bool, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}assignable": value}), Q()

    @filter_field
    def stateful(self, info: Info, queryset: QuerySet[models.Action], value: bool, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}stateful": value}), Q()

    @filter_field(description="Filter using app identifier")
    def app_identifier(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}app__identifier": value}), Q()
