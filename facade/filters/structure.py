# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for what services host: structures and their descriptors. Hub-wide."""

from __future__ import annotations

import strawberry
import strawberry_django
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field

from facade import models


@strawberry_django.order_type(models.Descriptor)
class StructureDescriptorOrder:
    key: auto
    type: auto


@strawberry_django.filter_type(models.Descriptor, description="A way to filter descriptors")
class StructureDescriptorFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.Descriptor], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Keep descriptors whose key or description contains this text")
    def search(self, info: Info, queryset: QuerySet[models.Descriptor], value: str, prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset, Q(**{f"{prefix}key__icontains": value}) | Q(**{f"{prefix}description__icontains": value})

    @filter_field(description="Filter by the exact descriptor key, e.g. '@mikro/n_channels'")
    def key(self, info: Info, queryset: QuerySet[models.Descriptor], value: str, prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}key": value}), Q()

    @filter_field(description="Filter by what the value is: INT, FLOAT, STRING, BOOL, LIST or ANY")
    def type(self, info: Info, queryset: QuerySet[models.Descriptor], value: list[str], prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}type__in": [kind.upper() for kind in value]}), Q()

    @filter_field(description="Keep the descriptors of this structure, by its identifier, e.g. '@mikro/arraydataset'")
    def structure(self, info: Info, queryset: QuerySet[models.Descriptor], value: str, prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}structure__identifier__iexact": value}), Q()

    @filter_field(description="Keep the descriptors of structures in this package, e.g. 'mikro'")
    def package(self, info: Info, queryset: QuerySet[models.Descriptor], value: str, prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}structure__identifier__istartswith": f"@{value}/"}), Q()

    @filter_field(description="Keep the descriptors declared by this service, by its name")
    def service(self, info: Info, queryset: QuerySet[models.Descriptor], value: str, prefix: str) -> tuple[QuerySet[models.Descriptor], Q]:
        return queryset.filter(**{f"{prefix}structure__service__name": value}), Q()


@strawberry_django.order_type(models.StructureDeclaration)
class HostedStructureOrder:
    identifier: auto
    label: auto


@strawberry_django.filter_type(models.StructureDeclaration, description="A way to filter hosted structures")
class HostedStructureFilter:
    @filter_field(description="Filter by IDs")
    def ids(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field(description="Keep structures whose identifier, label or description contains this text")
    def search(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: str, prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset, Q(**{f"{prefix}identifier__icontains": value}) | Q(**{f"{prefix}label__icontains": value}) | Q(**{f"{prefix}description__icontains": value})

    @filter_field(description="Keep the structures of this package, e.g. 'mikro'")
    def package(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: str, prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset.filter(**{f"{prefix}identifier__istartswith": f"@{value}/"}), Q()

    @filter_field(description="Keep the structures this service hosts, by its name")
    def service(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: str, prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset.filter(**{f"{prefix}service__name": value}), Q()

    @filter_field(description="Keep the structures whose objects carry this descriptor key")
    def descriptor(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: str, prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset.filter(**{f"{prefix}descriptors__key": value}).distinct(), Q()

    @filter_field(description="Keep the structures that declare descriptors (true), or those that declare none (false)")
    def described(self, info: Info, queryset: QuerySet[models.StructureDeclaration], value: bool, prefix: str) -> tuple[QuerySet[models.StructureDeclaration], Q]:
        return queryset.filter(**{f"{prefix}descriptors__isnull": not value}).distinct(), Q()
