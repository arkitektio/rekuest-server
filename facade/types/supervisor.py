"""The apps that provisioned the organization's agents: their supervisors."""

from __future__ import annotations

import strawberry
from django.db.models import Count
from kante.types import Info
from strawberry_django.filters import apply as apply_filters

from facade import filters, liveness, models
from facade.types.base import build_prescoped_queryset


@strawberry.type(
    description="An app that provisioned agents of the organization (a plugin engine, a launcher), as lok signed it into their tokens. Its agents are `agents(filters: {supervisor: identifier})`."
)
class Supervisor:
    identifier: str = strawberry.field(
        description="The app's identifier, as the agents carry it in `supervisor` (e.g. 'plugin_engine')."
    )
    agent_count: int = strawberry.field(
        description="How many of the organization's agents it provisioned (of those the filters keep)."
    )
    connected_count: int = strawberry.field(
        description="How many of those have a live connection now."
    )


def supervisors(
    info: Info, filters: filters.AgentFilter | None = None
) -> list[Supervisor]:
    """Every app that provisioned an agent of the caller's organization, by identifier."""
    queryset = build_prescoped_queryset(
        info, models.Agent.objects.all(), field="organization"
    )
    if filters is not None:
        queryset = apply_filters(filters, queryset, info)
    rows = (
        queryset.filter(supervisor__isnull=False)
        .values("supervisor")
        .annotate(
            agent_count=Count("id"),
            connected_count=Count("id", filter=liveness.agent_live_q()),
        )
        .order_by("supervisor")
    )
    return [
        Supervisor(
            identifier=row["supervisor"],
            agent_count=row["agent_count"],
            connected_count=row["connected_count"],
        )
        for row in rows
    ]


__all__ = ["Supervisor", "supervisors"]
