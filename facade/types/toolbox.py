"""Collections, protocols, toolboxes and shortcuts."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.filters.toolbox import pinned_for_caller
from facade.types.base import build_prescoped_queryset
from rekuest_core import scalars as rscalars
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.action import Action
    from facade.types.auth import User
    from facade.types.implementation import Implementation


@strawberry_django.type(models.Collection, description="A grouping of actions.")
class Collection:
    id: strawberry.ID = strawberry_django.field(description="Collection ID.")
    name: str = strawberry_django.field(description="Name of the collection.")
    actions: list["Action"] = strawberry_django.field(description="Actions included in this collection.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Collection], info: Info, **kwargs: object) -> QuerySet[models.Collection]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.Protocol, filters=filters.ProtocolFilter, pagination=True, ordering=filters.ProtocolOrder, description="A set of related actions forming a protocol.")
class Protocol:
    id: strawberry.ID = strawberry_django.field(description="Protocol ID.")
    name: str = strawberry_django.field(description="Name of the protocol.")
    actions: list["Action"] = strawberry_django.field(description="Associated actions.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Protocol], info: Info, **kwargs: object) -> QuerySet[models.Protocol]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.Toolbox, filters=filters.ToolboxFilter, pagination=True, ordering=filters.ToolboxOrder, description="A collection of shortcuts grouped as a toolbox.")
class Toolbox:
    id: strawberry.ID = strawberry_django.field(description="Toolbox ID.")
    name: str = strawberry_django.field(description="Name of the toolbox.")
    description: str = strawberry_django.field(description="Description of the toolbox.")
    shortcuts: list["Shortcut"] = strawberry_django.field(description="List of shortcuts in this toolbox.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Toolbox], info: Info, **kwargs: object) -> QuerySet[models.Toolbox]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.Shortcut, filters=filters.ShortcutFilter, pagination=True, ordering=filters.ShortcutOrder, description="Shortcut to an action with preset arguments.")
class Shortcut:
    id: strawberry.ID = strawberry_django.field(description="Shortcut ID.")
    name: str = strawberry_django.field(description="Name of the shortcut.")
    description: str | None = strawberry_django.field(description="Optional description.")
    action: "Action" = strawberry_django.field(description="The associated action.")
    implementation: Optional["Implementation"] = strawberry_django.field(description="Implementation of the action.")
    toolbox: "Toolbox" = strawberry_django.field(description="The toolbox this shortcut belongs to.")
    saved_args: rscalars.AnyDefault = strawberry_django.field(description="Saved arguments for the shortcut.")
    allow_quick: bool = strawberry_django.field(description="Allow quick execution without modification.")
    use_returns: bool = strawberry_django.field(description="If true, shortcut uses return values.")
    bind_number: int | None = strawberry_django.field(
        default=None,
        description="Which shortcut should be bound to this Action by default. 0 means no binding.",
    )

    bindings: list["ShortcutBinding"] = strawberry_django.field(description="Who an admin pinned this shortcut for, and where (app, client, device).")

    @strawberry_django.field(description="Whether an admin pinned this shortcut for the caller: one of its bindings matches who they are and where they call from. The caller cannot unpin it.")
    def pinned(self, info: Info) -> bool:
        annotated = getattr(self, "pinned_for_caller", None)
        if annotated is not None:
            return bool(annotated)
        return models.Shortcut.objects.filter(pk=self.pk).filter(pinned_for_caller(info)).exists()

    @strawberry_django.field(description="Input ports for the shortcut's action.dd")
    def args(self) -> list[rtypes.ArgPort]:
        return cast("list[rtypes.ArgPort]", [rmodels.ArgPortModel(**i) for i in self.args])

    @strawberry_django.field(description="Return ports from the shortcut's action.")
    def returns(self) -> list[rtypes.ReturnPort]:
        return cast("list[rtypes.ReturnPort]", [rmodels.ReturnPortModel(**i) for i in self.returns])

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Shortcut], info: Info, **kwargs: object) -> QuerySet[models.Shortcut]:
        return build_prescoped_queryset(info, queryset, field="toolbox__organization").annotate(pinned_for_caller=pinned_for_caller(info))


@strawberry_django.type(models.ShortcutBinding, description="An admin's pin of a shortcut, for whoever matches it: each of user, app, client and device that is set must be the caller's.")
class ShortcutBinding:
    id: strawberry.ID = strawberry_django.field(description="Binding ID.")
    shortcut: Shortcut = strawberry_django.field(description="The pinned shortcut.")
    user: Optional["User"] = strawberry_django.field(description="The user it is pinned for; null for every user.")
    app: str | None = strawberry_django.field(description="The identifier of the app it is pinned in; null for every app.")
    client: str | None = strawberry_django.field(description="The `client_id` of the client it is pinned in; null for every client.")
    device: str | None = strawberry_django.field(description="The `device_id` of the device it is pinned on; null for every device.")
    bound_by: "User" = strawberry_django.field(description="The admin who pinned it.")
    created_at: datetime = strawberry_django.field(description="When it was pinned.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.ShortcutBinding], info: Info, **kwargs: object) -> QuerySet[models.ShortcutBinding]:
        return build_prescoped_queryset(info, queryset, field="shortcut__toolbox__organization")
