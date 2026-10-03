"""Filters and orders for the automation rules (schedules, triggers) and for signals.

Schedules and triggers are filtered alike (both are rules that run an action); the fields are
written out on each, since a filter type does not take them from a plain base class.
"""

from __future__ import annotations

import datetime

import strawberry
import strawberry_django
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import enums, models


def _rule_search(prefix: str, text: str, *own: str) -> Q:
    """A rule mentions ``text``: itself, what it runs, or where it came from."""
    fields = ("name", "description", "interface", "action__name", "action__description", "agent__name", "wiregram__name", "wiregram__key", *own)
    found = Q()
    for field in fields:
        found |= Q(**{f"{prefix}{field}__icontains": text})
    return found


@strawberry_django.order_type(models.Schedule)
class ScheduleOrder:
    name: auto
    created_at: auto
    updated_at: auto


@strawberry_django.filter_type(models.Schedule, description="A way to filter schedules")
class ScheduleFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Keep schedules that mention this text: in their name or description, in the action or agent they run, its interface, their cron line, or the wiregram they came from")
    def search(self, info: Info, queryset, value: str, prefix: str):
        return queryset, _rule_search(prefix, value, "cron")

    @filter_field(description="Keep only enabled (true) or only disabled (false) rules")
    def enabled(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}enabled": value}), Q()

    @filter_field(description="Filter by the action the rule runs")
    def action(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}action_id": value}), Q()

    @filter_field(description="Filter by the agent the rule's runs are pinned to")
    def agent(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Keep only rules whose last run or firing failed (true), or only those in good standing (false)")
    def failing(self, info: Info, queryset, value: bool, prefix: str):
        lookup = {f"{prefix}consecutive_failures__gt": 0}
        return (queryset.filter(**lookup) if value else queryset.exclude(**lookup)), Q()


@strawberry_django.order_type(models.Trigger)
class TriggerOrder:
    name: auto
    created_at: auto
    updated_at: auto


@strawberry_django.filter_type(models.Trigger, description="A way to filter triggers")
class TriggerFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Keep triggers that mention this text: in their name or description, in the action or agent they run, its interface, the structure they listen for, its port, or the wiregram they came from")
    def search(self, info: Info, queryset, value: str, prefix: str):
        return queryset, _rule_search(prefix, value, "identifier", "port")

    @filter_field(description="Keep only enabled (true) or only disabled (false) rules")
    def enabled(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}enabled": value}), Q()

    @filter_field(description="Filter by the action the rule runs")
    def action(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}action_id": value}), Q()

    @filter_field(description="Filter by the agent the rule's runs are pinned to")
    def agent(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Keep only rules whose last run or firing failed (true), or only those in good standing (false)")
    def failing(self, info: Info, queryset, value: bool, prefix: str):
        lookup = {f"{prefix}consecutive_failures__gt": 0}
        return (queryset.filter(**lookup) if value else queryset.exclude(**lookup)), Q()

    @filter_field(description="Filter by the signal kind the trigger reacts to")
    def kind(self, info: Info, queryset, value: enums.SignalKind, prefix: str):
        return queryset.filter(**{f"{prefix}kind": value.value}), Q()

    @filter_field(description="Filter by the structure identifier the trigger reacts to")
    def identifier(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}identifier": value}), Q()


@strawberry_django.order_type(models.Signal)
class SignalOrder:
    received_at: auto
    occurred_at: auto


@strawberry_django.filter_type(models.Signal, description="A way to filter signals")
class SignalFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by what happened to the object")
    def kind(self, info: Info, queryset, value: list[enums.SignalKind], prefix: str):
        return queryset.filter(**{f"{prefix}kind__in": [kind.value for kind in value]}), Q()

    @filter_field(description="Keep signals that mention this text: in the structure identifier, the object's id, the sending service, or anywhere in the descriptors (keys and values)")
    def search(self, info: Info, queryset, value: str, prefix: str):
        queryset = queryset.alias(_descriptor_text=Cast(f"{prefix}descriptors", TextField()))
        return queryset, Q(**{f"{prefix}identifier__icontains": value}) | Q(**{f"{prefix}object__icontains": value}) | Q(**{f"{prefix}service__icontains": value}) | Q(_descriptor_text__icontains=value)

    @filter_field(description="Filter by the object's structure identifier")
    def identifier(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}identifier": value}), Q()

    @filter_field(description="Filter by the object's id within its structure")
    def object(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}object": value}), Q()

    @filter_field(description="Filter by the service that sent the signal")
    def service(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}service": value}), Q()

    @filter_field(description="Keep only signals triggers were already matched against (true), or only those still waiting (false)")
    def processed(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}processed_at__isnull": not value}), Q()

    @filter_field(description="Keep only signals that caused at least one run (true), or only those that caused none (false)")
    def matched(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}tasks__isnull": not value}).distinct(), Q()

    @filter_field(description="Only signals received before this timestamp")
    def received_before(self, info: Info, queryset, value: datetime.datetime, prefix: str):
        return queryset.filter(**{f"{prefix}received_at__lt": value}), Q()

    @filter_field(description="Only signals received after this timestamp")
    def received_after(self, info: Info, queryset, value: datetime.datetime, prefix: str):
        return queryset.filter(**{f"{prefix}received_at__gt": value}), Q()


@strawberry_django.order_type(models.Firing)
class FiringOrder:
    created_at: auto


@strawberry_django.filter_type(models.Firing, description="A way to filter the firing log")
class FiringFilter:
    @filter_field(description="Keep the firings of this trigger")
    def trigger(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}trigger_id": value}), Q()

    @filter_field(description="Keep the firings for this signal")
    def signal(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}signal_id": value}), Q()

    @filter_field(description="Filter by what became of the trigger")
    def outcome(self, info: Info, queryset, value: list[enums.FiringOutcome], prefix: str):
        return queryset.filter(**{f"{prefix}outcome__in": [outcome.value for outcome in value]}), Q()

    @filter_field(description="Keep only replays (true) or only firings caused by a signal arriving (false)")
    def replay(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}replay": value}), Q()
