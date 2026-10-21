# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for bloks and materialized bloks."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from django.db.models.expressions import RawSQL
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import inputs, managers, models
from rekuest_core import enums as renums


def _bloks_for_demands(info: Info, value: list[inputs.PortDemandInput]) -> RawSQL:
    """The ids of the bloks whose args accept what ``value`` demands (``requires`` included)."""
    return managers.get_blok_port_demand_subquery([managers.PortDemand.of(demand) for demand in value], organization_id=info.context.request.organization.id)


@strawberry_django.order_type(models.MaterializedBlok)
class MaterializedBlokOrder:
    created_at: auto


@strawberry_django.filter_type(models.Blok, description="A way to filter bloks")
class BlokFilter:
    @filter_field(description="Only bloks an app provides (true) or only custom bloks users made (false).")
    def provided(self, info: Info, queryset: QuerySet[models.Blok], value: bool, prefix: str) -> tuple[QuerySet[models.Blok], Q]:
        return queryset.filter(**{f"{prefix}app__isnull": not value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.Blok], value: str | None, prefix: str) -> tuple[QuerySet[models.Blok], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()

    @filter_field(description="Only bloks whose args accept these demands: the bloks an item opens in. Same matching as `ActionFilter.demands`, so a match's `descriptors` are checked against each arg's `requires`. Only `ARGS` demands; a blok without args matches none.")
    def demands(self, info: Info, queryset: QuerySet[models.Blok], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Blok], Q]:
        if len(value) == 0:
            return queryset, Q()
        return queryset.filter(**{f"{prefix}id__in": _bloks_for_demands(info, value)}), Q()


@strawberry_django.filter_type(models.MaterializedBlok, description="A way to filter placements (space memberships)")
class MaterializedBlokFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by agent")
    def agent(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}agent_mappings__agent_id": value}), Q()

    @filter_field(description="Only agents' own materializations of the bloks they provide (true) or only ones users made (false).")
    def provided(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: bool, prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}declared_by__isnull": not value}), Q()

    @filter_field(description="Search by name")
    def search(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: str | None, prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()

    @filter_field(description="Only bloks that render in one of these contexts: HTML and SCENE are interfaces to open, LOGO and ACTION_LOGO draw an agent or an action.")
    def contexts(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: list[renums.ComponentContext], prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        """Keep the materialized bloks whose blok renders in one of ``value``."""
        return queryset.filter(**{f"{prefix}blok__context__in": [context.value for context in value]}), Q()

    @filter_field(description="Only materialized bloks whose blok's args accept these demands: what an item opens in. See `BlokFilter.demands`.")
    def demands(self, info: Info, queryset: QuerySet[models.MaterializedBlok], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.MaterializedBlok], Q]:
        if len(value) == 0:
            return queryset, Q()
        return queryset.filter(**{f"{prefix}blok_id__in": _bloks_for_demands(info, value)}), Q()
