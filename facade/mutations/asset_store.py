"""Uploading the files an agent declares, and reading what the organization holds."""

from dataclasses import asdict
from typing import cast

from graphql import GraphQLError
from kante.types import Info

from datalayer import base_models
from datalayer import types as dtypes
from datalayer.datalayer import get_current_datalayer
from datalayer.models import MediaStore
from facade import asset_store, inputs, models, types
from facade.asset_store import AssetRefused
from facade.caller_context import CallerContext
from facade.image_validation import IMAGE_FORMATS
from rekuest_core.enums import AssetKind

CONTENT_TYPES = {AssetKind.MODEL_3D.value: "model/gltf-binary"}


def _refusal(error: AssetRefused) -> GraphQLError:
    """A refused asset as a GraphQL error whose ``extensions.code`` clients switch on."""
    return GraphQLError(error.message, extensions={"code": error.code})


def _declared(info: Info, ref: str) -> models.AgentAsset:
    """The asset of this reference that the caller's own agent declares."""
    caller = CallerContext.from_info(info)
    return asset_store.declared_asset(
        organization_id=info.context.request.organization.id,
        client_id=caller.client.pk if caller.client is not None else None,
        user_id=caller.user.pk,
        ref=ref,
    )


def request_asset_upload(info: Info, input: inputs.RequestAssetUploadInput) -> dtypes.MediaUploadGrant:
    """A media grant for one asset, issued only if the caller's agent declares it and its size passes the policy."""
    try:
        declared = _declared(info, input.ref)
        asset_store.check_upload(declared)
    except AssetRefused as error:
        raise _refusal(error) from error
    grant = get_current_datalayer().generate_media_upload_grant(
        base_models.RequestMediaUploadInput(original_file_name=f"{declared.name}-{declared.ref.removeprefix('sha256:')[:16]}", file_size=declared.size, content_type=CONTENT_TYPES.get(declared.kind)),
        organization_id=info.context.request.organization.id,
        creator_id=info.context.request.user.id,
    )
    return dtypes.MediaUploadGrant(**grant.model_dump())


def create_asset(info: Info, input: inputs.CreateAssetInput) -> types.Asset:
    """Verify an uploaded asset and record it; the existing record if its hash is stored already."""
    try:
        store = MediaStore.objects.get(id=str(input.store), organization=info.context.request.organization)
    except MediaStore.DoesNotExist:
        raise PermissionError(f"No media store {input.store} in your organization.") from None
    try:
        asset = asset_store.create_asset(get_current_datalayer(), declared=_declared(info, input.ref), user_id=info.context.request.user.id, store=store)
    except AssetRefused as error:
        raise _refusal(error) from error
    return cast("types.Asset", asset)


def asset(info: Info, ref: str) -> types.Asset | None:
    """The organization's stored asset of this reference, or null. Another organization's copy is never revealed."""
    return cast("types.Asset | None", models.Asset.objects.filter(organization=info.context.request.organization, ref=ref).first())


def asset_policy(info: Info) -> types.AssetPolicy:
    """The limits each kind of asset is held to."""
    policy = asset_store.asset_policy()
    model = asdict(policy.model)
    return types.AssetPolicy(model=types.ModelPolicy(**{**model, "extensions": sorted(model["extensions"])}), image=types.ImagePolicy(**asdict(policy.image), formats=list(IMAGE_FORMATS)))


def bloks_for_model(info: Info, ref: str) -> list[types.Blok]:
    """The html bloks registered to decorate this model, by name."""
    return cast("list[types.Blok]", list(models.Blok.objects.filter(organization=info.context.request.organization, for_model=ref).order_by("name")))
