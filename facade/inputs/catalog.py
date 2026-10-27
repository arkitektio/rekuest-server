"""Inputs for registering modules: an app's own UI extras."""

import strawberry
from pydantic import BaseModel, Field, model_validator
from strawberry.experimental import pydantic
from typing_extensions import Self

from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs import types as ritypes


class RegisterModuleInputModel(BaseModel):
    """What a UI app can render and evaluate beyond the spec's catalogs, upserted by name within the caller's organization."""

    name: str = Field(min_length=1, description="The module name. Bloks (`module`) and definitions (`catalogs`) reference it by this name; registering again replaces the previous components and operations.")
    description: str | None = Field(default=None, description="Human-readable description of the module.")
    components: list[rimodels.CatalogComponentInputModel] = Field(default_factory=list, description="The components this module can render.")
    operations: list[rimodels.CatalogOperationInputModel] = Field(default_factory=list, description="The pure operations this module can evaluate for UtilCalls.")
    catalogs: list[str] = Field(
        default_factory=list,
        description="The spec's versioned catalogs (blok language v2) this UI implements whole, as name@version ('ui@1', 'scene@1'). Its `components` are what it draws beyond them.",
    )
    widget_defaults: list[rimodels.WidgetDefaultInputModel] = Field(
        default_factory=list,
        description="Default widgets per port kind and/or structure identifier. A UI renders them for ports that declare no widget; an identifier match beats a kind match. Each widget is validated against this module plus base at registration.",
    )

    @model_validator(mode="after")
    def check_catalogs(self) -> Self:
        """Each catalog is spelled name@version and named once (``manifest.catalog``, ``manifest.duplicate``)."""
        from rekuest_core.vocabulary import BlokError

        for catalog in self.catalogs:
            if not rimodels.CATALOG_PATTERN.fullmatch(catalog):
                raise BlokError("manifest.catalog", catalog=catalog)
        twice = next((catalog for catalog in self.catalogs if self.catalogs.count(catalog) > 1), None)
        if twice is not None:
            raise BlokError("manifest.duplicate", what="catalogs", key=twice)
        return self

    @model_validator(mode="after")
    def check_unique_selectors(self) -> Self:
        """Two defaults may not target the same (kind, identifier)."""
        seen: set[tuple[str | None, str | None]] = set()
        for default in self.widget_defaults:
            if default.selector in seen:
                raise ValueError(f"widget_defaults: duplicate selector kind={default.selector[0]!r} identifier={default.selector[1]!r}")
            seen.add(default.selector)
        return self


@pydantic.input(RegisterModuleInputModel, description="Register a module (upsert by name, scoped to the caller's organization): the components and operations a UI app can render and evaluate beyond the spec's catalogs.")
class RegisterModuleInput:
    name: str
    description: str | None = None
    components: list[ritypes.CatalogComponentInput] = strawberry.field(default_factory=list)
    operations: list[ritypes.CatalogOperationInput] = strawberry.field(default_factory=list)
    catalogs: list[str] = strawberry.field(default_factory=list)
    widget_defaults: list[ritypes.WidgetDefaultInput] = strawberry.field(default_factory=list)
