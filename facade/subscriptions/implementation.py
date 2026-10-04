from typing import AsyncGenerator, Optional, cast

import strawberry
from kante.types import Info

from facade import models, types
from facade.channels import new_implementation_channel


@strawberry.type(description="An implementation feed event: exactly one of create/update/delete is set.")
class ImplementationUpdate:
    create: Optional[types.Implementation] = None
    update: Optional[types.Implementation] = None
    delete: Optional[strawberry.ID] = None


async def implementations(
    self: object,
    info: Info,
    agent: strawberry.ID,
) -> AsyncGenerator[ImplementationUpdate, None]:
    """Subscribe to implementation create/update/delete for one agent."""
    async for message in new_implementation_channel.listen(info, [f"implementations_agent_{agent}"]):
        if message.create:
            yield ImplementationUpdate(create=cast("types.Implementation", await models.Implementation.objects.aget(id=message.create)))
        elif message.update:
            yield ImplementationUpdate(update=cast("types.Implementation", await models.Implementation.objects.aget(id=message.update)))
        elif message.delete:
            yield ImplementationUpdate(delete=strawberry.ID(str(message.delete)))


async def implementation_change(
    self: object,
    info: Info,
    implementation: strawberry.ID,
) -> AsyncGenerator[types.Implementation, None]:
    """Subscribe to updates of one implementation."""
    x = await models.Implementation.objects.aget(id=implementation)

    async for message in new_implementation_channel.listen(info, [f"implementation_{x.pk}"]):
        # Deletes end the row — nothing to yield for this row-typed stream.
        if message.update:
            yield cast("types.Implementation", await models.Implementation.objects.aget(id=message.update))
