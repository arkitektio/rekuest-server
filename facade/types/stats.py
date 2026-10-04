"""Statistics over the organization's actions and tasks: totals, and buckets over time.

A stats type is asked which field to aggregate (and, for a series, which timestamp to bucket by).
Every scalar of one field comes out of one aggregate query, made once per request however many
of them the query selects; a series is one ``GROUP BY`` over the truncated timestamp.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum

import strawberry
import strawberry_django
from django.db.models import Avg, Count, DateField, FloatField, Max, Min, Model, QuerySet, Sum
from django.db.models.functions import Extract, Trunc
from kante.types import Info
from strawberry_django.filters import apply as apply_filters

from facade import filters, models
from facade.types.base import build_prescoped_queryset


@strawberry.type
class TimeBucket:
    ts: datetime.datetime
    count: int
    distinctCount: int  # noqa: N815  the field's GraphQL name
    max: float | None
    min: float | None
    avg: float | None
    sum: float | None


@strawberry.enum
class Granularity(str, Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


@dataclass(frozen=True)
class Summary:
    """Every scalar statistic of one field."""

    distinct_count: int
    max: float | None
    min: float | None
    avg: float | None
    sum: float | None


class Selection[M: Model]:
    """The rows a stats field is about, and what was already aggregated over them."""

    def __init__(self, queryset: QuerySet[M]) -> None:
        self.queryset = queryset
        self._summaries: dict[str, Summary] = {}

    def _is_temporal(self, field: str) -> bool:
        """Whether ``field`` (which may span relations) is a date or a datetime."""
        options = self.queryset.model._meta
        found = None
        for part in field.split("__"):
            found = options.pk if part == "pk" else options.get_field(part)
            if found is not None and found.is_relation and found.related_model is not None and not isinstance(found.related_model, str):
                options = found.related_model._meta
        return isinstance(found, DateField)

    def _aggregates(self, field: str) -> dict[str, Max | Min | Avg | Sum]:
        """The max/min/avg/sum aggregates of the field.

        Postgres has no avg() or sum() over timestamps and the stats are floats, so a temporal
        field is aggregated as epoch seconds; its sum means nothing and is left out.
        """
        if self._is_temporal(field):
            epoch = Extract(field, "epoch", output_field=FloatField())
            return {"max": Max(epoch), "min": Min(epoch), "avg": Avg(epoch)}
        return {"max": Max(field), "min": Min(field), "avg": Avg(field), "sum": Sum(field)}

    def count(self) -> int:
        return self.queryset.count()

    def summary(self, field: str) -> Summary:
        """Every scalar statistic of ``field``, from one query, made once."""
        known = self._summaries.get(field)
        if known is None:
            row = self.queryset.aggregate(distinctCount=Count(field, distinct=True), **self._aggregates(field))
            known = Summary(distinct_count=row["distinctCount"], max=row["max"], min=row["min"], avg=row["avg"], sum=row.get("sum"))
            self._summaries[field] = known
        return known

    def series(self, field: str, timestamp_field: str, by: Granularity) -> list[TimeBucket]:
        """The statistics of ``field`` per bucket of ``timestamp_field``, oldest first."""
        rows = self.queryset.annotate(bucket=Trunc(timestamp_field, by.value)).values("bucket").annotate(count=Count("pk"), distinctCount=Count(field, distinct=True), **self._aggregates(field)).order_by("bucket")
        return [TimeBucket(ts=row["bucket"], count=row["count"], distinctCount=row["distinctCount"], max=row["max"], min=row["min"], avg=row["avg"], sum=row.get("sum")) for row in rows]


@strawberry.enum(description="Numeric/aggregatable fields of Action")
class ActionField(Enum):
    CREATED_AT = "defined_at"


@strawberry.enum(description="Datetime fields of Action for bucketing")
class ActionTimestampField(Enum):
    CREATED_AT = "defined_at"


@strawberry.type
class ActionStats:
    _selection: strawberry.Private[Selection[models.Action]]

    @strawberry_django.field(description="Total number of items in the selection")
    def count(self) -> int:
        return self._selection.count()

    @strawberry_django.field(description="Number of distinct values for the field")
    def distinct_count(self, field: ActionField) -> int:
        return self._selection.summary(field.value).distinct_count

    @strawberry_django.field(description="Maximum")
    def max(self, field: ActionField) -> float | None:
        return self._selection.summary(field.value).max

    @strawberry_django.field(description="Minimum")
    def min(self, field: ActionField) -> float | None:
        return self._selection.summary(field.value).min

    @strawberry_django.field(description="Average")
    def avg(self, field: ActionField) -> float | None:
        return self._selection.summary(field.value).avg

    @strawberry_django.field(description="Sum")
    def sum(self, field: ActionField) -> float | None:
        return self._selection.summary(field.value).sum

    @strawberry_django.field(description="Time-bucketed stats over a datetime field.")
    def series(self, field: ActionField, timestamp_field: ActionTimestampField, by: Granularity) -> list[TimeBucket]:
        return self._selection.series(field.value, timestamp_field.value, by)


def action_stats(info: Info, filters: filters.ActionFilter | None = None) -> ActionStats:
    """The statistics of the organization's actions, narrowed by ``filters``."""
    queryset = build_prescoped_queryset(info, models.Action.objects.all(), field="organization")
    if filters is not None:
        queryset = apply_filters(filters, queryset, info)
    return ActionStats(_selection=Selection(queryset))


@strawberry.enum(description="Numeric/aggregatable fields of Task")
class TaskField(Enum):
    CREATED_AT = "created_at"


@strawberry.enum(description="Datetime fields of Task for bucketing")
class TaskTimestampField(Enum):
    CREATED_AT = "created_at"


@strawberry.type
class TaskStats:
    _selection: strawberry.Private[Selection[models.Task]]

    @strawberry_django.field(description="Total number of items in the selection")
    def count(self) -> int:
        return self._selection.count()

    @strawberry_django.field(description="Number of distinct values for the field")
    def distinct_count(self, field: TaskField) -> int:
        return self._selection.summary(field.value).distinct_count

    @strawberry_django.field(description="Maximum")
    def max(self, field: TaskField) -> float | None:
        return self._selection.summary(field.value).max

    @strawberry_django.field(description="Minimum")
    def min(self, field: TaskField) -> float | None:
        return self._selection.summary(field.value).min

    @strawberry_django.field(description="Average")
    def avg(self, field: TaskField) -> float | None:
        return self._selection.summary(field.value).avg

    @strawberry_django.field(description="Sum")
    def sum(self, field: TaskField) -> float | None:
        return self._selection.summary(field.value).sum

    @strawberry_django.field(description="Time-bucketed stats over a datetime field.")
    def series(self, field: TaskField, timestamp_field: TaskTimestampField, by: Granularity) -> list[TimeBucket]:
        return self._selection.series(field.value, timestamp_field.value, by)


def task_stats(info: Info, filters: filters.TaskFilter | None = None) -> TaskStats:
    """The statistics of the organization's tasks, narrowed by ``filters``."""
    # Through the non-null ``agent`` relation, as the Task type scopes itself.
    queryset = build_prescoped_queryset(info, models.Task.objects.all(), field="agent__organization")
    if filters is not None:
        queryset = apply_filters(filters, queryset, info)
    return TaskStats(_selection=Selection(queryset))
