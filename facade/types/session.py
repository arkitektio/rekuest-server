"""Sessions and their boundaries."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import kante
import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.types.base import build_prescoped_queryset

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.state import Patch, Snapshot


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
    def get_queryset(cls, queryset: QuerySet[models.Session], info: Info, **kwargs: object) -> QuerySet[models.Session]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")
