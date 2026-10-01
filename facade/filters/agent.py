"""Filters and orders for agents and implementation-agents."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Exists, OuterRef, Q
from strawberry import UNSET, auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import managers, models
from rekuest_core.inputs import types as ritypes


@strawberry_django.filter_type(models.Agent, description="A way to filter agents")
class AgentFilter:
    @filter_field(description="Filter by client ID of the app the agent is registered to")
    def client_id(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}client__client_id": value}), Q()

    @filter_field(description="Filter by IDs of the agents")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by implementations of the agents")
    def has_implementations(self, info: Info, queryset, value: list[str], prefix: str):
        return queryset.filter(**{f"{prefix}implementations__action__hash__in": value}), Q()

    @filter_field(description="Filter by states of the agents")
    def has_states(self, info: Info, queryset, value: list[str], prefix: str):
        return queryset.filter(**{f"{prefix}states__definition__hash__in": value}), Q()

    @filter_field(description="Filter by pinned agents")
    def pinned(self, info: Info, queryset, value: bool, prefix: str):
        user = info.context.request.user
        if value:
            return queryset.filter(**{f"{prefix}pinned_by__id": user.id}), Q()
        else:
            return queryset.exclude(**{f"{prefix}pinned_by__id": user.id}), Q()

    @filter_field(description="Filter by name of the agents")
    def search(self, info: Info, queryset, value: str, prefix: str):
        if value == "":
            return queryset, Q()
        return queryset.filter(Q(**{f"{prefix}name__icontains": value}) | Q(**{f"{prefix}display_name__icontains": value})), Q()

    @filter_field
    def dependency(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        dep = models.Dependency.objects.get(id=value)
        if dep.app_filter is not UNSET and dep.app_filter is not None:
            return queryset.filter(**{f"{prefix}app__identifier": dep.app_filter}), Q()
        else:
            raise ValueError("Filtering by dependency currently only allowed when the dependency declared an app filter, sorry :( This is coming")

    @filter_field
    def blok_dependency(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        dep = models.BlokDependency.objects.get(id=value)
        if dep.app_filter is not UNSET and dep.app_filter is not None:
            return queryset.filter(**{f"{prefix}app__identifier": dep.app_filter}), Q()
        else:
            raise ValueError("Filtering by blok_dependency currently only allowed when the blok_dependency declared an app filter, sorry :( This is coming")

    @filter_field
    def three_d_model(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        dep = models.ThreeDModel.objects.get(id=value)
        if self.app_identifier is not UNSET and self.app_identifier is not None:
            return queryset.filter(**{f"{prefix}app__identifier": dep.app_filter}), Q()
        else:
            raise ValueError("Filtering by three_d_model currently only allowed when also filtering by app identifier")

    @filter_field
    def distinct(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.distinct(), Q()

    @filter_field
    def action_demands(self, info: Info, queryset, value: list[ritypes.ActionDemandInput], prefix: str):
        # One matcher round trip for all demands. The agent must satisfy EVERY demand, but each
        # demand may be met by a different implementation — hence one Exists() per demand
        # (ANDed) rather than one merged id set or chained M2M joins (which multiply rows).
        per_demand_ids = managers.get_action_ids_by_action_demands(
            value,
            organization_id=info.context.request.organization.id,
        )

        for ports_demand, new_ids in zip(value, per_demand_ids):
            if len(new_ids) == 0:
                raise ValueError(f"No actions found that match the given action demands {ports_demand}")

            queryset = queryset.filter(Exists(models.Implementation.objects.filter(agent=OuterRef(f"{prefix}pk"), action_id__in=new_ids)))

        return queryset, Q()

    @filter_field
    def state_demands(self, info: Info, queryset, value: list[ritypes.StateDemandInput], prefix: str):
        # The agent must satisfy EVERY state demand (one Exists() per demand, ANDed) — each
        # demand may be met by a different State of the agent, mirroring action_demands.
        # app/key match the State's identity columns; matches resolve via the port matcher.
        for state_demand in value:
            queryset = queryset.filter(Exists(models.State.objects.filter(agent=OuterRef(f"{prefix}pk"), **managers.state_demand_state_filters(state_demand))))

        return queryset, Q()

    @filter_field(description="Filter by user ID")
    def user(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}user__sub": value}), Q()

    @filter_field(description="Filter using app identifier")
    def app_identifier(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}app__identifier": value}), Q()

    @filter_field(description="Filter based on version string")
    def version_number(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}release__version": value}), Q()

    @filter_field(description="Filter based on device")
    def device_id(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}client__device__device_id": value}), Q()


@strawberry_django.order_type(models.Agent)
class AgentOrder:
    last_seen: auto


@strawberry_django.filter_type(models.Agent)
class ImplementationAgentFilter:
    @filter_field
    def client_id(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}client__client_id": value}), Q()

    @filter_field
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def has_implementations(self, info: Info, queryset, value: list[str], prefix: str):
        return queryset.filter(**{f"{prefix}implementations__action__hash__in": value}), Q()

    @filter_field
    def has_states(self, info: Info, queryset, value: list[str], prefix: str):
        return queryset.filter(**{f"{prefix}states__definition__hash__in": value}), Q()
