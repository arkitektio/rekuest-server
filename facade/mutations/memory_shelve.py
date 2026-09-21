import strawberry
from kante.types import Info

from facade import models, registration, types
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


def _agent_of(info: Info) -> models.Agent:
    request = info.context.request
    return registration.ensure_agent(request.client, request.user, request.organization)


def shelve_in_memory_drawer(info: Info, input: ShelveInMemoryDrawerInput) -> types.MemoryDrawer:
    """Record a value the caller's agent holds in memory (the GraphQL twin of ``Shelve``)."""
    return registration.shelve(
        _agent_of(info),
        identifier=input.identifier,
        resource_id=input.resource_id,
        label=input.label,
        description=input.description,
    )


@strawberry.input
class UnshelveMemoryDrawerInput:
    id: str = strawberry.field(description="The resource ID of the drawer.")


def unshelve_memory_drawer(info: Info, input: UnshelveMemoryDrawerInput) -> strawberry.ID:
    """Drop a drawer from the caller's agent's shelve (the GraphQL twin of ``Unshelve``)."""
    registration.unshelve(_agent_of(info), input.id)
    return input.id
