"""Test case and test result types."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.types.base import build_prescoped_queryset
from rekuest_core import scalars as rscalars

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.action import Action
    from facade.types.implementation import Implementation


@strawberry_django.type(models.TestCase, filters=filters.TestCaseFilter, pagination=True, description="Defines a test case comparing expected behavior for actions.")
class TestCase:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the test case.")
    tester: "Action" = strawberry_django.field(description="Action used to perform the test.")
    action: "Action" = strawberry_django.field(description="Target action under test.")
    is_benchmark: bool = strawberry_django.field(description="If true, measures performance rather than correctness.")
    description: str = strawberry_django.field(description="Details of what this test case covers.")
    name: str = strawberry_django.field(description="Short name for the test case.")
    results: list["TestResult"] = strawberry_django.field(description="Results from running this test case.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.TestCase], info: Info, **kwargs: object) -> QuerySet[models.TestCase]:
        return build_prescoped_queryset(info, queryset, field="action__organization")


@strawberry_django.type(models.TestResult, filters=filters.TestResultFilter, pagination=True, description="Result from executing a test case with specific implementations.")
class TestResult:
    id: strawberry.ID = strawberry_django.field(description="ID of the test result.")
    implementation: "Implementation" = strawberry_django.field(description="Implementation under test.")
    tester: "Implementation" = strawberry_django.field(description="Implementation running the test.")
    case: "TestCase" = strawberry_django.field(description="Associated test case.")
    passed: bool = strawberry_django.field(description="True if test passed.")
    result: rscalars.AnyDefault | None = strawberry_django.field(description="What the test produced, as JSON.")
    created_at: datetime.datetime = strawberry_django.field(description="When the test was executed.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.TestResult], info: Info, **kwargs: object) -> QuerySet[models.TestResult]:
        return build_prescoped_queryset(info, queryset, field="case__action__organization")
