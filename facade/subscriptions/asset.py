from typing import AsyncGenerator

import strawberry
from kante.types import Info

from facade.channels import asset_channel, assets_org


@strawberry.type(description="An asset that was just stored: enough to tell whether it is the one awaited.")
class AssetChange:
    id: strawberry.ID
    ref: str = strawberry.field(description="'sha256:' and the hex SHA-256 of the file's bytes: what an agent's declared asset names.")
    kind: str = strawberry.field(description="What the file is (AssetKind).")


async def assets(
    self: object,
    info: Info,
    refs: list[str] | None = None,
) -> AsyncGenerator[AssetChange, None]:
    """The organization's assets as they are stored, only those of ``refs`` if given."""
    wanted = set(refs) if refs is not None else None
    async for message in asset_channel.listen(info, [assets_org(info.context.request.organization.id)]):
        if wanted is None or message.ref in wanted:
            yield AssetChange(id=strawberry.ID(str(message.create)), ref=message.ref, kind=message.kind)
