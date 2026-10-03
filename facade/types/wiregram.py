"""The Wiregram type: an imported automation document and the rules it owns."""

from __future__ import annotations

import datetime

import strawberry
import strawberry_django
from rekuest_core import scalars as rscalars

from facade import models
from facade.types.base import build_prescoped_queryset


@strawberry_django.type(models.Wiregram, pagination=True, description="An automation document an organization imported, and the owner of the schedules and triggers it created.")
class Wiregram:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the wiregram.")
    key: str = strawberry_django.field(description="What the document calls itself; importing the same key again updates this wiregram.")
    name: str = strawberry_django.field(description="Human-readable name.")
    description: str | None = strawberry_django.field(description="What the document says it is for.")
    document: rscalars.AnyDefault = strawberry_django.field(description="The document as it was last imported: importable as it is.")
    caller: "Caller" = strawberry_django.field(description="Who imported it last; the runs of its rules are assigned as this identity.")
    created_at: datetime.datetime = strawberry_django.field(description="When it was first imported.")
    updated_at: datetime.datetime = strawberry_django.field(description="When it was last imported.")
    schedules: list["Schedule"] = strawberry_django.field(description="The schedules it owns.")
    triggers: list["Trigger"] = strawberry_django.field(description="The triggers it owns.")

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="organization")
