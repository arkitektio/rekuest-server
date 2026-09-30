"""Sessions and their boundaries."""

from __future__ import annotations

import datetime

import kante
import strawberry
import strawberry_django

from facade import filters, models
from facade.types.base import build_prescoped_queryset


@strawberry.type
class TaskBoundary:
    correlation_id: str
    start_global_revision: int | None
    end_global_revision: int | None
    start_time: datetime.datetime | None
    end_time: datetime.datetime | None


@strawberry.type
class SessionBoundary:
    session_id: str
    start_global_revision: int | None
    end_global_revision: int | None
    start_time: datetime.datetime | None
    end_time: datetime.datetime | None


@kante.django_type(
    models.Session,
    filters=filters.SessionFilter,
    ordering=filters.SessionOrder,
    pagination=True,
    description="A session representing a continuous interaction of an agent with the system.",
)
class Session:
    id: strawberry.ID
    agent: Agent
    started_at: datetime.datetime = strawberry_django.field(field_name="created_at", description="When the session started.")
    snapshots: list[Snapshot]
    patches: list[Patch]

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="agent__organization")


