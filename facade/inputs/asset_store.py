"""Inputs for the asset store."""

import strawberry

from datalayer import scalars as dscalars


@strawberry.input(description="Ask for a grant to upload one asset the caller's agent declares.")
class RequestAssetUploadInput:
    ref: str = strawberry.field(description="The asset's reference, as the agent declared it: 'sha256:' and the hex SHA-256 of its exact bytes.")


@strawberry.input(description="Record an asset that was uploaded with a grant from `requestAssetUpload`.")
class CreateAssetInput:
    ref: str = strawberry.field(description="The reference the uploaded bytes must hash to.")
    store: dscalars.MediaLike = strawberry.field(description="The store the grant was issued for.")
