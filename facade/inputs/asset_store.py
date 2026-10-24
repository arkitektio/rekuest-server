"""Inputs for the asset store."""

import strawberry

from arkitekt_datalayer import scalars as dscalars


@strawberry.input(description="Ask for a grant to upload one asset: one the caller's agent declares, or, for an admin, one an agent of the organization names a source for.")
class RequestAssetUploadInput:
    ref: str = strawberry.field(description="The asset's reference, as the agent declared it: 'sha256:' and the hex SHA-256 of its exact bytes.")
    size: int | None = strawberry.field(default=None, description="The size of the fetched file in bytes, for an asset declared without one. Ignored when the declaration has a size.")


@strawberry.input(description="Record an asset that was uploaded with a grant from `requestAssetUpload`.")
class CreateAssetInput:
    ref: str = strawberry.field(description="The reference the uploaded bytes must hash to.")
    store: dscalars.MediaLike = strawberry.field(description="The store the grant was issued for.")
