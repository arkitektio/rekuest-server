# pyright: reportExplicitAny=false
# strawberry-django's `auto` (a field whose type comes from the model) is itself spelled `Any`.
"""Filters and orders for authentication/organization models."""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from authentikate.models import Client, Organization, User
from django.db.models import Q, QuerySet
from strawberry import auto
from strawberry.types import Info
from strawberry_django.fields.filter_order import filter_field
from strawberry_django.filters import FilterLookup

from rekuest_core import scalars as rscalars


@strawberry_django.filter_type(User)
class UserFilter:
    name: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[User], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[User], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.order_type(User)
class UserOrder:
    name: auto
    email: auto
    date_joined: auto
    last_login: auto


@strawberry_django.order_type(Organization, description="A way to order registries")
class OrganizationOrder:
    slug: auto


@strawberry_django.filter_type(Organization, description="A way to filter organizations")
class OrganizationFilter:
    slug: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[Organization], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[Organization], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()


@strawberry_django.order_type(Client, description="A way to order apps")
class ClientOrder:
    defined_at: auto


@strawberry_django.filter_type(Client, description="A way to filter apps")
class ClientFilter:
    interface: Optional[FilterLookup[str]]

    @filter_field
    def ids(self, info: Info, queryset: QuerySet[Client], value: list[strawberry.ID], prefix: str) -> tuple[QuerySet[Client], Q]:
        return queryset.filter(**{f"{prefix}id__in": value}), Q()

    @filter_field
    def has_implementations_for(self, info: Info, queryset: QuerySet[Client], value: list[rscalars.ActionHash], prefix: str) -> tuple[QuerySet[Client], Q]:
        return queryset.filter(**{f"{prefix}agents__implementations__action__hash__in": value}).distinct(), Q()

    @filter_field
    def mine(self, info: Info, queryset: QuerySet[Client], value: bool, prefix: str) -> tuple[QuerySet[Client], Q]:
        return queryset.filter(**{f"{prefix}user_id": info.context.user.id}), Q()
