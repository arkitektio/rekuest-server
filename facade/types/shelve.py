"""Memory and filesystem shelves and their drawers."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.types.base import build_prescoped_queryset

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent


@strawberry_django.type(models.MemoryShelve, filters=filters.MemoryShelveFilter, ordering=filters.MemoryShelveOrder, pagination=True, description="A shelve for storing memory-based resources on an agent.")
class MemoryShelve:
    id: strawberry.ID = strawberry_django.field(description="ID of the memory shelve.")
    agent: "Agent" = strawberry_django.field(description="Agent that owns this memory shelve.")
    name: str = strawberry_django.field(description="Name of the shelve.")
    description: str | None = strawberry_django.field(description="Optional description of the shelve.")
    drawers: list["MemoryDrawer"] = strawberry_django.field(description="List of memory drawers within the shelve.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.MemoryShelve], info: Info, **kwargs: object) -> QuerySet[models.MemoryShelve]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry_django.type(models.MemoryDrawer, filters=filters.MemoryDrawerFilter, pagination=True)
class MemoryDrawer:
    id: strawberry.ID
    resource_id: str
    shelve: "MemoryShelve"
    identifier: str
    description: str | None
    created_at: datetime.datetime
    agent_minted: bool = strawberry_django.field(description="Whether the agent minted this drawer's reference: it is addressed by resourceId rather than by id.")

    @strawberry_django.field(description="Get the latest value stored in this drawer.")
    def label(self) -> str:
        return self.label or self.identifier + "@" + self.resource_id

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.MemoryDrawer], info: Info, **kwargs: object) -> QuerySet[models.MemoryDrawer]:
        return build_prescoped_queryset(info, queryset, field="shelve__organization")
