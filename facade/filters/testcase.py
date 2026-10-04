"""Filters for test cases and test results."""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from facade import models


@strawberry_django.filter_type(models.TestCase, description="A way to filter test cases")
class TestCaseFilter:
    name: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.TestCase], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.TestCase], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.filter_type(models.TestResult, description="A way to filter test results")
class TestResultFilter:
    passed: Optional[bool]

    @filter_field(description="Results whose test case's name contains this")
    def search(self, info: Info, queryset: QuerySet[models.TestResult], value: str, prefix: str) -> tuple[QuerySet[models.TestResult], Q]:
        return queryset.filter(**{f"{prefix}case__name__icontains": value}), Q()

    @filter_field(description="Results of this test case")
    def case(self, info: Info, queryset: QuerySet[models.TestResult], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.TestResult], Q]:
        return queryset.filter(**{f"{prefix}case_id": value}), Q()

    @filter_field(description="Results of the test cases of this action")
    def action(self, info: Info, queryset: QuerySet[models.TestResult], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.TestResult], Q]:
        return queryset.filter(**{f"{prefix}case__action_id": value}), Q()

    @filter_field(description="Results for this implementation under test")
    def implementation(self, info: Info, queryset: QuerySet[models.TestResult], value: strawberry.ID, prefix: str) -> tuple[QuerySet[models.TestResult], Q]:
        return queryset.filter(**{f"{prefix}implementation_id": value}), Q()

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.TestResult], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.TestResult], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()
