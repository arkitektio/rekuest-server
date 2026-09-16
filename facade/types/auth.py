"""Authentication and organization GraphQL types."""

from __future__ import annotations

import kante
import strawberry
import strawberry_django
from authentikate import models as auth_models

from facade import filters, models


@kante.django_type(auth_models.User, filters=filters.UserFilter, pagination=True, ordering=filters.UserOrder, description="Represents an authenticated user.")
class User:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the user.")
    sub: strawberry.ID = strawberry_django.field(description="The subject identifier of the user.")


@strawberry_django.type(auth_models.Device, description="Represents a device assigned to users within an organization.")
class Device:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the device.")
    device_id: strawberry.ID = strawberry_django.field(description="The device identifier.")


@strawberry_django.type(auth_models.App, description="Profile information for a user.")
class App:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the app.")
    identifier: str = strawberry_django.field(description="Name of the app.")


@strawberry_django.type(auth_models.Release, description="Profile information for a user.")
class Release:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the release.")
    app: App = strawberry_django.field(description="The app this release belongs to.")
    version: str = strawberry_django.field(description="Version string of the release.")


@strawberry_django.type(auth_models.Client, filters=filters.ClientFilter, pagination=True, ordering=filters.ClientOrder, description="Represents a registered OAuth2 client.")
class Client:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the client.")
    client_id: str = strawberry_django.field(description="OAuth2 client ID.")
    release: Release | None = strawberry_django.field(description="Release associated with the client.")
    device: Device | None = strawberry_django.field(description="Device associated with the client.")


@strawberry_django.type(auth_models.Organization, filters=filters.OrganizationFilter, pagination=True, ordering=filters.OrganizationOrder, description="Represents an organization in the system.")
class Organization:
    slug: str = strawberry_django.field(description="Slug of the organization.")


@strawberry_django.type(models.Caller, description="The (client, user, organization) identity that requests work.")
class Caller:
    id: strawberry.ID = strawberry_django.field(description="Unique identifier for the caller.")
    client: Client = strawberry_django.field(description="The associated client.")
    user: User = strawberry_django.field(description="The associated user.")
    organization: Organization = strawberry_django.field(description="The organization this caller belongs to.")
