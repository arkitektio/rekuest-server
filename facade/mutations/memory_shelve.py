from typing import cast

import strawberry
from kante.types import Info

from facade import models, takt, takt_api, types
from facade.caller_context import CallerContext
from facade.takt_api import Principal
from rekuest_core import scalars as rscalars


@strawberry.input
class ShelveInMemoryDrawerInput:
    identifier: rscalars.Identifier = strawberry.field(description="The identifier of the drawer. This is used to identify the drawer in the system.")
    resource_id: str = strawberry.field(description="The resource ID of the drawer.")
    label: str | None = strawberry.field(
        default=None,
        description="The label of the drawer. This is used to identify the drawer in the system.",
    )
    description: str | None = strawberry.field(
        default=None,
        description="The description of the drawer. This is used to identify the drawer in the system.",
    )


def shelve_in_memory_drawer(info: Info, input: ShelveInMemoryDrawerInput) -> types.MemoryDrawer:
    """Record a value the caller's agent holds in memory (the GraphQL twin of ``Shelve``), in takt."""
    request = takt_api.ShelveRequest(
        principal=Principal.of(CallerContext.from_info(info)),
        identifier=input.identifier,
        resource_id=input.resource_id,
        label=input.label,
        description=input.description,
    )
    return cast("types.MemoryDrawer", models.MemoryDrawer.objects.get(pk=takt.call(takt_api.SHELVE, request).drawer))


@strawberry.input
class UnshelveMemoryDrawerInput:
    id: str = strawberry.field(description="The drawer: its resource ID (as agent-minted drawers are referenced) or its ID.")


def unshelve_memory_drawer(info: Info, input: UnshelveMemoryDrawerInput) -> strawberry.ID:
    """Drop a drawer from the caller's agent's shelve (the GraphQL twin of ``Unshelve``), in takt.

    ``id`` is looked up as a resource ID on the caller's agent's shelve first, then as a pk.
    """
    takt.call(takt_api.UNSHELVE, takt_api.UnshelveRequest(principal=Principal.of(CallerContext.from_info(info)), id=input.id))
    return strawberry.ID(input.id)
