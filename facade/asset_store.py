"""The asset store: the files agents declare, verified and kept once per organization.

An agent names its files at registration (``AgentAsset``). It may then upload the ones
the organization does not hold, with a grant issued only for a file its own declaration
names: a kulisse grant for a 3D model, a media grant for an image. A file it does not hold it
names a ``source`` for instead, and an admin's client fetches and uploads that one. Nothing
about either upload is trusted: ``create_asset`` finishes a model's kulisse store (whose own
reader checks the GLB and hashes it) and reads an image back, and holds each to its kind.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from arkitekt_datalayer.datalayer import Datalayer, load_config
from arkitekt_datalayer.kulisse import KulissePolicy, KulisseRefused, check_ref
from arkitekt_datalayer.models import DatalayerStore, KulisseStore, MediaStore
from django.conf import settings
from django.db import IntegrityError, transaction

from facade import models
from facade.image_validation import ImagePolicy, ImageRefused, validate_image
from rekuest_core.enums import AssetKind


class AssetRefused(ValueError):
    """A file that may not enter the store, with the code clients switch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: Texture limits a client checks before it uploads (it decodes the images; the server never
#: does). Advertised beside the server's own model policy so a client holds one set of numbers.
MAX_TEXTURE_SIZE = 4096
MAX_TEXTURE_MEMORY = 256 * 1024 * 1024


@dataclass(frozen=True)
class AssetPolicy:
    """The limits each kind of asset is held to: a model's are the datalayer's kulisse policy."""

    model: KulissePolicy
    image: ImagePolicy

    def max_size(self, kind: str) -> int:
        """The largest file of ``kind``."""
        return self.model.max_size if kind == AssetKind.MODEL_3D.value else self.image.max_size


def _overlaid[P](policy: type[P], setting: str) -> P:  # noqa: D103
    """``policy``'s defaults overlaid with the deployment's ``setting``."""
    overrides = dict(getattr(settings, setting, None) or {})
    unknown = sorted(set(overrides) - {field.name for field in fields(policy)})  # type: ignore[arg-type]
    if unknown:
        raise ValueError(f"{setting} has unknown keys {unknown}")
    if "extensions" in overrides:
        overrides["extensions"] = frozenset(overrides["extensions"])
    return policy(**overrides)


def asset_policy() -> AssetPolicy:
    """The deployment's policy: ``datalayer.kulisse_policy`` for models, ``settings.IMAGE_POLICY`` over the defaults for images."""
    return AssetPolicy(model=load_config().kulisse_policy.policy(), image=_overlaid(ImagePolicy, "IMAGE_POLICY"))


def _check_ref(ref: str) -> None:
    try:
        check_ref(ref)
    except KulisseRefused as error:
        raise AssetRefused("bad_reference", f"an asset reference is 'sha256:' and 64 lowercase hex characters, not {ref!r}") from error


def declared_asset(*, organization_id: int, client_id: int | None, user_id: int, ref: str) -> models.AgentAsset:
    """What the caller's own agent declares for ``ref``: the only files a caller may upload."""
    _check_ref(ref)
    declared = models.AgentAsset.objects.filter(agent__organization_id=organization_id, agent__client_id=client_id, agent__user_id=user_id, ref=ref).order_by("id").first()
    if declared is None:
        raise AssetRefused("not_declared", f"your agent declares no asset {ref}: an app uploads the files it registered, after it registered")
    return declared


def sourced_asset(*, organization_id: int, ref: str) -> models.AgentAsset:
    """What any agent of the organization declares for ``ref`` with a source: the files an admin may upload for it."""
    _check_ref(ref)
    declared = models.AgentAsset.objects.filter(agent__organization_id=organization_id, ref=ref, source__isnull=False).order_by("id").first()
    if declared is None:
        raise AssetRefused("not_sourced", f"no agent of your organization names a source for the asset {ref}: an admin uploads only the files an app says where to fetch")
    return declared


def upload_size(declared: models.AgentAsset, measured: int | None) -> int:
    """The size a grant is issued for: the declared one, else what the uploader measured. Refused if over the limit."""
    size = declared.size if declared.size is not None else measured
    limit = asset_policy().max_size(declared.kind)
    if size is None or size <= 0:
        raise AssetRefused("bad_header", "an asset has a size")
    if size > limit:
        raise AssetRefused("too_large", f"the asset '{declared.name}' is {size} bytes, over the limit of {limit}")
    return size


def _read_object(datalayer: Datalayer, store: MediaStore, limit: int) -> bytes:
    """The stored bytes, refused before they are read if the object is over ``limit``."""
    try:
        size = store.measure_bytes(datalayer)
    except FileNotFoundError as error:
        raise AssetRefused("not_uploaded", f"nothing was uploaded to store {store.pk}") from error
    if size > limit:
        raise AssetRefused("too_large", f"the file is {size} bytes, over the limit of {limit}")
    return store.read_bytes(datalayer, max_bytes=limit + 1)


def _finish_model(datalayer: Datalayer, store: DatalayerStore, ref: str) -> tuple[int, dict[str, Any]]:
    """Finish a model's kulisse store -- its reader checks the GLB and hashes it -- and hold it to ``ref``."""
    if not isinstance(store, KulisseStore):
        raise AssetRefused("wrong_store", f"a 3D model is uploaded to a kulisse store (requestAssetUpload issues one), not to store {store.pk}")
    try:
        store.fill_info(datalayer)
    except FileNotFoundError as error:
        raise AssetRefused("not_uploaded", f"nothing was uploaded to store {store.pk}") from error
    except KulisseRefused as error:
        raise AssetRefused(error.code, error.message) from error
    if store.ref != ref:
        raise AssetRefused("hash_mismatch", f"the stored bytes are {store.ref}, not {ref}")
    return int(store.size_bytes or 0), {"nodes": store.nodes or [], "stats": store.stats, "joints": store.joints or []}


def _finish_image(datalayer: Datalayer, store: DatalayerStore, ref: str, policy: AssetPolicy) -> tuple[int, dict[str, Any]]:
    """Read an image back and hold it to the image rules and ``ref``."""
    if not isinstance(store, MediaStore):
        raise AssetRefused("wrong_store", f"an image is uploaded to a media store (requestAssetUpload issues one), not to store {store.pk}")
    try:
        image = validate_image(_read_object(datalayer, store, policy.image.max_size), ref, policy.image)
    except ImageRefused as error:
        raise AssetRefused(error.code, error.message) from error
    store.fill_info(datalayer)
    return image.size, {"format": image.format, "width": image.width, "height": image.height}


def create_asset(datalayer: Datalayer, *, declared: models.AgentAsset, user_id: int, store: DatalayerStore) -> models.Asset:
    """Verify what was uploaded to ``store`` and record it; the existing record if the hash is stored already.

    A refused upload leaves nothing behind: the object and its store row are deleted.
    """
    organization_id = declared.agent.organization_id
    existing = models.Asset.objects.filter(organization_id=organization_id, ref=declared.ref).first()
    if existing is not None:
        if existing.file_id != store.pk:
            store.delete()
        return existing

    try:
        if declared.kind == AssetKind.MODEL_3D.value:
            size, meta = _finish_model(datalayer, store, declared.ref)
        else:
            size, meta = _finish_image(datalayer, store, declared.ref, asset_policy())
    except AssetRefused:
        # Deleting the store row deletes its object.
        store.delete()
        raise

    try:
        with transaction.atomic():
            return models.Asset.objects.create(organization_id=organization_id, ref=declared.ref, kind=declared.kind, file=store, size=size, meta=meta, uploaded_by_id=user_id)
    except IntegrityError:
        # Two agents uploaded the same file at once: one record, the other upload is surplus.
        store.delete()
        return models.Asset.objects.get(organization_id=organization_id, ref=declared.ref)
