"""Filters and orders for tasks, task events and task instructs."""

from __future__ import annotations

import datetime

import strawberry
import strawberry_django
from django.db.models import Q
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import enums, models


@strawberry_django.order_type(models.Task)
class TaskOrder:
    created_at: auto
    finished_at: auto


@strawberry_django.filter_type(models.Task, description="A way to filter tasks")
class TaskFilter:
    @filter_field(description="Filter by IDs of the tasks")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by the client ID of the app the executing agent is registered to")
    def client_id(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}agent__client__client_id": value}), Q()

    @filter_field(description="Filter by the latest lifecycle event of the task")
    def state(self, info: Info, queryset, value: list[enums.TaskEventKind], prefix: str):
        return queryset.filter(**{f"{prefix}latest_event_kind__in": value}), Q()

    @filter_field(description="Filter by the implementation the task is currently mapped to")
    def implementation(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}implementation_id": value}), Q()

    @filter_field(description="Filter by the action the task was assigned to")
    def action(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}action_id": value}), Q()

    @filter_field(description="Filter by the agent executing the task")
    def agent(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}agent_id": value}), Q()

    @filter_field(description="Filter by the caller (client/user/organization) that created the task")
    def caller(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}caller_id": value}), Q()

    @filter_field(description="Filter by the direct parent task")
    def parent(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}parent_id": value}), Q()

    @filter_field(description="Filter by the root task of the execution tree")
    def root(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}root_id": value}), Q()

    @filter_field(description="Keep only root tasks (true) or only descendants of a root (false)")
    def root_isnull(self, info: Info, queryset, value: bool, prefix: str):
        # ``root``, not ``parent``: a deeper descendant has a non-null ``root`` but its ``parent``
        # points at an intermediate task, so ``parent__isnull`` would not identify roots. This
        # matches ``queries.my_tasks`` and the ``tasks`` subscription.
        return queryset.filter(**{f"{prefix}root__isnull": value}), Q()

    @filter_field(description="Filter by whether the task has finished")
    def is_done(self, info: Info, queryset, value: bool, prefix: str):
        return queryset.filter(**{f"{prefix}is_done": value}), Q()

    @filter_field(description="Filter by the structures this task acted on")
    def acted_on(self, info: Info, queryset, value: list[str], prefix: str):
        return queryset.filter(**{f"{prefix}acted_on__overlap": value}), Q()

    @filter_field(description="Filter by the canonical args hash (the replay-discovery key)")
    def args_hash(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}args_hash": value}), Q()

    @filter_field(description="Filter by the caller-supplied reference of the task")
    def reference(self, info: Info, queryset, value: str, prefix: str):
        return queryset.filter(**{f"{prefix}reference": value}), Q()

    @filter_field(description="Keep the runs of this schedule")
    def schedule(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}schedule_id": value}), Q()

    @filter_field(description="Keep the runs this trigger fired")
    def trigger(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}trigger_id": value}), Q()

    @filter_field(description="Keep the runs this signal caused")
    def signal(self, info: Info, queryset, value: strawberry.ID, prefix: str):
        return queryset.filter(**{f"{prefix}signal_id": value}), Q()

    @filter_field(description="Only tasks created before this timestamp")
    def created_before(self, info: Info, queryset, value: datetime.datetime, prefix: str):
        return queryset.filter(**{f"{prefix}created_at__lt": value}), Q()

    @filter_field(description="Only tasks created after this timestamp")
    def created_after(self, info: Info, queryset, value: datetime.datetime, prefix: str):
        return queryset.filter(**{f"{prefix}created_at__gt": value}), Q()


@strawberry_django.order_type(models.TaskEvent)
class TaskEventOrder:
    created_at: auto
    agent_pos: auto
    step: auto


@strawberry_django.filter_type(models.TaskEvent, description="A way to filter task events")
class TaskEventFilter:
    @filter_field(description="Filter by IDs of the task events")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by the kind of the event")
    def kind(self, info: Info, queryset, value: list[enums.TaskEventKind], prefix: str):
        return queryset.filter(**{f"{prefix}kind__in": value}), Q()


@strawberry_django.filter_type(models.TaskInstruct, description="A way to filter task instructs")
class TaskInstructFilter:
    """``TaskInstruct.kind`` is populated from ``TaskInstructChoices``, not ``TaskEventChoices`` —
    filtering it through ``TaskEventFilter`` matched nothing and returned silently empty results."""

    @filter_field(description="Filter by IDs of the task instructs")
    def ids(self, info: Info, queryset, value: list[strawberry.ID], prefix: str):
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Filter by the kind of the instruction")
    def kind(self, info: Info, queryset, value: list[enums.TaskInstructKind], prefix: str):
        return queryset.filter(**{f"{prefix}kind__in": value}), Q()
