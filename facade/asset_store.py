"""The asset store: the files agents declare, verified and kept once per organization.

An agent names its files at registration (``AgentAsset``). It may then upload the ones
the organization does not hold: with an ordinary media grant, issued only for a file its
own declaration names. A file it does not hold it names a ``source`` for instead, and an
admin's client fetches and uploads that one. Nothing about either upload is trusted.
``create_asset`` reads the stored object back and holds it to the rules of its kind.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from arkitekt_datalayer.datalayer import Datalayer
from arkitekt_datalayer.models import MediaStore
from django.conf import settings
from django.db import IntegrityError, transaction

from facade import models
from facade.glb_validation import ModelPolicy, ModelRefused, check_ref_format, validate_model
from facade.image_validation import ImagePolicy, ImageRefused, validate_image
from rekuest_core.enums import AssetKind


class AssetRefused(ValueError):
    """A file that may not enter the store, with the code clients switch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AssetPolicy:
    """The limits each kind of asset is held to."""

    model: ModelPolicy
    image: ImagePolicy

    def max_size(self, kind: str) -> int:
        """The largest file of ``kind``."""
        return self.model.max_size if kind == AssetKind.MODEL_3D.value else self.image.max_size


def _overlaid[P](policy: type[P], setting: str) -> P:
    """``policy``'s defaults overlaid with the deployment's ``setting``."""
    overrides = dict(getattr(settings, setting, None) or {})
    unknown = sorted(set(overrides) - {field.name for field in fields(policy)})  # type: ignore[arg-type]
    if unknown:
        raise ValueError(f"{setting} has unknown keys {unknown}")
    if "extensions" in overrides:
        overrides["extensions"] = frozenset(overrides["extensions"])
    return policy(**overrides)


def asset_policy() -> AssetPolicy:
    """The deployment's policy: the defaults overlaid with ``settings.MODEL_POLICY`` and ``settings.IMAGE_POLICY``."""
    return AssetPolicy(model=_overlaid(ModelPolicy, "MODEL_POLICY"), image=_overlaid(ImagePolicy, "IMAGE_POLICY"))


def _check_ref(ref: str) -> None:
    try:
        check_ref_format(ref)
    except ModelRefused as error:
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


def _validate(data: bytes, ref: str, kind: str, policy: AssetPolicy) -> tuple[int, dict[str, Any]]:
    """Hold ``data`` to the rules of ``kind``: its size and what validation found."""
    try:
        if kind == AssetKind.MODEL_3D.value:
            model = validate_model(data, ref, policy.model)
            return model.size, {"nodes": model.nodes, "stats": asdict(model.stats)}
        image = validate_image(data, ref, policy.image)
        return image.size, {"format": image.format, "width": image.width, "height": image.height}
    except (ModelRefused, ImageRefused) as error:
        raise AssetRefused(error.code, error.message) from error


def create_asset(datalayer: Datalayer, *, declared: models.AgentAsset, user_id: int, store: MediaStore) -> models.Asset:
    """Verify what was uploaded to ``store`` and record it; the existing record if the hash is stored already.

    A refused upload leaves nothing behind: the object and its store row are deleted.
    """
    organization_id = declared.agent.organization_id
    existing = models.Asset.objects.filter(organization_id=organization_id, ref=declared.ref).first()
    if existing is not None:
        if existing.file_id != store.pk:
            store.delete()
        return existing

    policy = asset_policy()
    try:
        size, meta = _validate(_read_object(datalayer, store, policy.max_size(declared.kind)), declared.ref, declared.kind, policy)
    except AssetRefused:
        # Deleting the store row deletes its object.
        store.delete()
        raise

    store.fill_info(datalayer)
    try:
        with transaction.atomic():
            return models.Asset.objects.create(organization_id=organization_id, ref=declared.ref, kind=declared.kind, file=store, size=size, meta=meta, uploaded_by_id=user_id)
    except IntegrityError:
        # Two agents uploaded the same file at once: one record, the other upload is surplus.
        store.delete()
        return models.Asset.objects.get(organization_id=organization_id, ref=declared.ref)
