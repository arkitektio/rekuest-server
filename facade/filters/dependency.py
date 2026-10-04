"""Filters for dependencies, resolved dependencies and resolutions."""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from facade import models


@strawberry_django.filter_type(models.Resolution, description="A way to filter test cases")
class ResolutionFilter:
    name: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Resolution], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Resolution], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.filter_type(models.ResolvedDependency, description="A way to filter resolved dependencies")
class ResolvedDependencyFilter:
    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.ResolvedDependency], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.ResolvedDependency], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.filter_type(models.Dependency)
class DependencyFilter:
    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.Dependency], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Dependency], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.filter_type(models.BlokDependency)
class BlokDependencyFilter:
    @filter_field
    def ids(self, info: Info, queryset: QuerySet[models.BlokDependency], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.BlokDependency], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()
