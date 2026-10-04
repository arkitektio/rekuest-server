# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for implementations (and the implementation-action filter)."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Optional

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from django.utils import timezone
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from facade import inputs, managers, models
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.inputs import types as ritypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.filters.agent import ImplementationAgentFilter
    from facade.filters.common import ParamPair


@strawberry_django.filter_type(models.Action)
class ImplementationActionFilter:
    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}name__icontains": value}), Q()

    @filter_field
    def name(self, info: Info, queryset: QuerySet[models.Action], value: str, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}name": value}), Q()

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Action], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def demands(self, info: Info, queryset: QuerySet[models.Action], value: list[inputs.PortDemandInput], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        if len(value) == 0:
            return queryset, Q()

        # RawSQL subquery: one round trip, no id materialization (see ActionFilter.demands).
        subquery = managers.get_action_port_demand_subquery([managers.PortDemand.of(demand) for demand in value], organization_id=info.context.request.organization.id)
        return queryset.filter(**{f"{prefix}id__in": subquery}), Q()

    @filter_field
    def kind(self, info: Info, queryset: QuerySet[models.Action], value: renums.ActionKind, prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}kind": value}), Q()

    @filter_field
    def protocols(self, info: Info, queryset: QuerySet[models.Action], value: list[str], prefix: str) -> tuple[QuerySet[models.Action], Q]:
        return queryset.filter(**{f"{prefix}protocols__name__in": value}), Q()


@strawberry_django.order_type(models.Implementation)
class ImplementationOrder:
    created_at: auto

    @strawberry_django.order_field
    def active(self, value: strawberry_django.Ordering, prefix: str) -> list[str]:
        if not value:
            return []
        return [f"{prefix}agent__connected"]


@strawberry_django.filter_type(models.Implementation)
class ImplementationFilter:
    interface: Optional[FilterLookup[str]]
    action: ImplementationActionFilter | None
    agent: ImplementationAgentFilter | None

    @filter_field
    def active(self, info: Info, queryset: QuerySet[models.Implementation], value: bool, prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        now = timezone.now()
        if value:
            return queryset.filter(**{f"{prefix}agent__last_seen__gt": now - datetime.timedelta(minutes=5)}), Q()
        else:
            return queryset.filter(**{f"{prefix}agent__last_seen__lte": now - datetime.timedelta(minutes=5)}), Q()

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Implementation], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def action_hash(self, info: Info, queryset: QuerySet[models.Implementation], value: rscalars.ActionHash, prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        return queryset.filter(**{f"{prefix}action__hash": value}), Q()

    @filter_field
    def parameters(self, info: Info, queryset: QuerySet[models.Implementation], value: list[ParamPair], prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        for param in value:
            queryset = queryset.filter(**{f"{prefix}params__contains": {param.key: param.value}})
        return queryset, Q()

    @filter_field
    def resolvable_for(self, info: Info, queryset: QuerySet[models.Implementation], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        dependency = models.Dependency.objects.get(id=value)
        return queryset.filter(**{f"{prefix}action__app__identifier": dependency.app_filter}), Q()

    @filter_field
    def search(self, info: Info, queryset: QuerySet[models.Implementation], value: str, prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        return queryset.filter(Q(**{f"{prefix}action__name__icontains": value}) | Q(**{f"{prefix}agent__name__icontains": value}) | Q(**{f"{prefix}interface__icontains": value})), Q()

    @filter_field
    def action_demand(self, info: Info, queryset: QuerySet[models.Implementation], value: ritypes.ActionDemandInput, prefix: str) -> tuple[QuerySet[models.Implementation], Q]:
        new_ids = managers.get_action_ids_by_action_demands(
            [value.to_pydantic()],
            organization_id=info.context.request.organization.id,
        )[0]
        return queryset.filter(**{f"{prefix}action__id__in": new_ids}), Q()
