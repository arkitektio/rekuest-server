"""Assets: the files agents bring, what the organization holds of them, and the limits they are held to."""

from __future__ import annotations

import datetime

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from datalayer import types as dtypes
from facade import models
from facade.types.base import build_prescoped_queryset, row_of
from rekuest_core import enums


@strawberry.type(description="What validation counted in a 3D model.")
class ModelStats:
    nodes: int
    triangles: int
    textures: int
    animations: int


@strawberry.type(description="The limits a 3D model is held to when it is added to the store.")
class ModelPolicy:
    max_size: float = strawberry.field(description="Largest file, in bytes.")
    max_json: int = strawberry.field(description="Largest JSON chunk, in bytes.")
    max_nodes: int
    max_triangles: int
    max_texture_size: int = strawberry.field(description="Largest texture side, in pixels. Checked by clients, which decode the images.")
    max_texture_memory: float = strawberry.field(description="Decoded texture memory in bytes (width x height x 4, summed). Checked by clients.")
    max_animations: int
    extensions: list[str] = strawberry.field(description="The glTF extensions a model may require.")


@strawberry.type(description="The limits an image is held to when it is added to the store.")
class ImagePolicy:
    max_size: float = strawberry.field(description="Largest file, in bytes.")
    max_side: int = strawberry.field(description="Largest width or height, in pixels.")
    formats: list[str] = strawberry.field(description="The formats an image may be in.")


@strawberry.type(description="The limits each kind of asset is held to.")
class AssetPolicy:
    model: ModelPolicy
    image: ImagePolicy


@strawberry_django.type(models.Asset, description="A verified file the organization holds, stored once by its content.")
class Asset:
    id: strawberry.ID
    ref: str = strawberry_django.field(description="'sha256:' and the hex SHA-256 of the file's exact bytes.")
    kind: enums.AssetKind = strawberry_django.field(description="What the file is.")
    file: dtypes.MediaStore
    size: int = strawberry_django.field(description="The size of the file in bytes, measured by the server.")
    created_at: datetime.datetime

    @strawberry_django.field(description="A 3D model's node names: what blok bindings can address. Empty for other kinds.", only=["meta"])
    def nodes(self) -> list[str]:
        return list(row_of(self, models.Asset).meta.get("nodes") or [])

    @strawberry_django.field(description="A 3D model's node, triangle, texture and animation counts. Null for other kinds.", only=["meta"])
    def stats(self) -> ModelStats | None:
        stats = row_of(self, models.Asset).meta.get("stats")
        return ModelStats(**stats) if stats else None

    @strawberry_django.field(description="An image's width in pixels. Null for other kinds.", only=["meta"])
    def width(self) -> int | None:
        return row_of(self, models.Asset).meta.get("width")

    @strawberry_django.field(description="An image's height in pixels. Null for other kinds.", only=["meta"])
    def height(self) -> int | None:
        return row_of(self, models.Asset).meta.get("height")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Asset], info: Info, **kwargs: object) -> QuerySet[models.Asset]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.AgentAsset, description="A file an agent brings, under the name its bloks call it by (`@<dependency>.assets.<name>`).")
class AgentAsset:
    id: strawberry.ID
    name: str = strawberry_django.field(description="The name bloks refer to the asset by.")
    kind: enums.AssetKind = strawberry_django.field(description="What the agent says the file is.")
    ref: str = strawberry_django.field(description="'sha256:' and the hex SHA-256 of the file's exact bytes, as the agent declares it.")
    size: int | None = strawberry_django.field(description="The size of the file in bytes, as the agent declares it; null for a file it does not hold.")
    source: str | None = strawberry_django.field(description="The https URL the file can be fetched from, for a file the agent does not hold: an admin's client fetches and uploads it.")

    @strawberry_django.field(description="The stored file, once it was uploaded; null until then.", only=["ref", "agent__organization_id"], select_related=["agent"])
    def asset(self) -> Asset | None:
        row = row_of(self, models.AgentAsset)
        return models.Asset.objects.filter(organization_id=row.agent.organization_id, ref=row.ref).first()  # type: ignore[return-value]

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.AgentAsset], info: Info, **kwargs: object) -> QuerySet[models.AgentAsset]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")
