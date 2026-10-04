"""Dashboards, dashboard placements and UI catalogs."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.types.base import build_prescoped_queryset
from rekuest_core.catalogs import load_base_catalog
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.blok import Blok, MaterializedBlok


@strawberry_django.type(models.Dashboard)
class Dashboard:
    id: strawberry.ID
    name: str | None
    placements: list["DashboardPlacement"]

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Dashboard], info: Info, **kwargs: object) -> QuerySet[models.Dashboard]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.UICatalog, description="A UI catalog: the components a UI app can render and the pure operations it can evaluate for UtilCalls, registered per organization.")
class UICatalog:
    id: strawberry.ID
    name: str
    description: str | None
    bloks: list["Blok"] = strawberry_django.field(description="Bloks rendered against this catalog.")

    @strawberry_django.field(description="Registered components. Empty until a UI app registers the catalog.")
    def components(self) -> list[rtypes.CatalogComponent]:
        return cast("list[rtypes.CatalogComponent]", [rmodels.CatalogComponentModel(**component) for component in self.components])

    @strawberry_django.field(description="Registered pure operations UtilCalls may name. Empty until a UI app registers the catalog.")
    def operations(self) -> list[rtypes.CatalogOperation]:
        return cast("list[rtypes.CatalogOperation]", [rmodels.CatalogOperationModel(**operation) for operation in self.operations])

    @strawberry_django.field(description="Whether a UI app has registered components or operations; unregistered catalogs validate nothing.")
    def is_registered(self) -> bool:
        return self.is_registered

    @strawberry_django.field(description="Default widgets per port kind and/or structure identifier. A UI renders them for ports that declare no widget; an identifier match beats a kind match.")
    def widget_defaults(self) -> list[rtypes.WidgetDefault]:
        return cast("list[rtypes.WidgetDefault]", [rmodels.WidgetDefaultModel(**default) for default in self.widget_defaults])

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.UICatalog], info: Info, **kwargs: object) -> QuerySet[models.UICatalog]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry.type(description="The built-in base catalog: the pure operations every UI implements, applied to every definition and blok before any registered UI catalog. Virtual: shipped with the server, not registered, identical in every organization.")
class BaseCatalog:
    name: str = strawberry.field(description="Always 'base'.")
    version: int = strawberry.field(description="Manifest version; evolves additively.")
    description: str | None = strawberry.field(default=None, description="What the base catalog is for.")
    operations: list[rtypes.CatalogOperation] = strawberry.field(description="The base operations, argument order being positional order.")


def base_catalog(info: Info) -> BaseCatalog:
    """The base catalog manifest as GraphQL."""
    manifest = load_base_catalog()
    return BaseCatalog(
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        operations=cast("list[rtypes.CatalogOperation]", [rmodels.CatalogOperationModel(**operation.model_dump()) for operation in manifest.operations]),
    )


@strawberry_django.type(
    models.DashboardPlacement,
    filters=filters.DashboardPlacementFilter,
    ordering=filters.DashboardPlacementOrder,
    pagination=True,
    description="A placement of an agent in a space.",
)
class DashboardPlacement:
    id: strawberry.ID
    dashboard: Dashboard
    blok: MaterializedBlok | None
