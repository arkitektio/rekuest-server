"""Uploading the files an agent declares, and reading what the organization holds."""

from dataclasses import asdict
from typing import cast

from arkitekt_datalayer import base_models
from arkitekt_datalayer.context import caller
from arkitekt_datalayer.datalayer import get_current_datalayer
from arkitekt_datalayer.models import DatalayerStore
from authentikate.strawberry.directives import has_org_role
from graphql import GraphQLError
from kante.types import Info

from facade import asset_store, inputs, models, types
from facade.asset_store import AssetRefused
from facade.caller_context import CallerContext
from facade.image_validation import IMAGE_FORMATS
from rekuest_core.enums import AssetKind


def _refusal(error: AssetRefused) -> GraphQLError:
    """A refused asset as a GraphQL error whose ``extensions.code`` clients switch on."""
    return GraphQLError(error.message, extensions={"code": error.code})


def _declared(info: Info, ref: str) -> models.AgentAsset:
    """The declaration the caller may upload ``ref`` for.

    The caller's own agent's, as ever. An admin of the organization may also upload a
    file that any of its agents names a source for: that agent holds no bytes to upload.
    """
    caller = CallerContext.from_info(info)
    organization_id = info.context.request.organization.id
    try:
        return asset_store.declared_asset(
            organization_id=organization_id,
            client_id=caller.client.pk if caller.client is not None else None,
            user_id=caller.user.pk,
            ref=ref,
        )
    except AssetRefused as refusal:
        if refusal.code != "not_declared" or not has_org_role(info, "admin"):
            raise
        return asset_store.sourced_asset(organization_id=organization_id, ref=ref)


def request_asset_upload(info: Info, input: inputs.RequestAssetUploadInput) -> types.AssetUploadGrant:
    """A grant for one asset -- a kulisse grant for a 3D model, a media grant for an image -- issued only if the caller may upload it and its size passes the policy."""
    try:
        declared = _declared(info, input.ref)
        size = asset_store.upload_size(declared, input.size)
    except AssetRefused as error:
        raise _refusal(error) from error
    organization_id, user = caller(info)
    datalayer = get_current_datalayer()
    file_name = f"{declared.name}-{declared.ref.removeprefix('sha256:')[:16]}"
    if declared.kind == AssetKind.MODEL_3D.value:
        grant: base_models.BaseUploadGrant = datalayer.generate_kulisse_upload_grant(organization_id, base_models.RequestKulisseUploadInput(original_file_name=file_name, file_size=size), user=user)
    else:
        grant = datalayer.generate_media_upload_grant(organization_id, base_models.RequestMediaUploadInput(original_file_name=file_name, file_size=size), user=user)
    return types.AssetUploadGrant(**grant.model_dump())


def create_asset(info: Info, input: inputs.CreateAssetInput) -> types.Asset:
    """Verify an uploaded asset and record it; the existing record if its hash is stored already."""
    try:
        store = DatalayerStore.objects.get(id=str(input.store), organization=info.context.request.organization)
    except DatalayerStore.DoesNotExist:
        raise PermissionError(f"No store {input.store} in your organization.") from None
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
    return types.AssetPolicy(
        model=types.ModelPolicy(**{**model, "extensions": sorted(model["extensions"])}, max_texture_size=asset_store.MAX_TEXTURE_SIZE, max_texture_memory=asset_store.MAX_TEXTURE_MEMORY),
        image=types.ImagePolicy(**asdict(policy.image), formats=list(IMAGE_FORMATS)),
    )


def bloks_for_model(info: Info, ref: str) -> list[types.Blok]:
    """The html bloks registered to decorate this model, by name."""
    return cast("list[types.Blok]", list(models.Blok.objects.filter(organization=info.context.request.organization, for_model=ref).order_by("name")))
