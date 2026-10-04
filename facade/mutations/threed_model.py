"""3D models: a media file, how an agent's state drives it, and which agent that is."""

from typing import cast

import strawberry
from kante.types import Info
from pydantic import BaseModel

from datalayer.models import MediaStore
from facade import inputs, models, types
from facade.json_types import JSON
from facade.types.base import scoped_get


def _media(info: Info, media: str) -> MediaStore:
    try:
        return MediaStore.objects.get(id=media, organization=info.context.request.organization)
    except MediaStore.DoesNotExist:
        raise PermissionError(f"No media store {media} in your organization.") from None


def _dump(dependency: BaseModel | None) -> JSON:
    """A model's dependency as the document it is stored as."""
    return dependency.model_dump(mode="json") if dependency is not None else None


def create_threed_model(info: Info, input: inputs.CreateThreeDModelInput) -> types.ThreeDModel:
    """Create a 3D model in the caller's organization."""
    data = input.to_pydantic()
    return cast(
        "types.ThreeDModel",
        models.ThreeDModel.objects.create(
            name=data.name,
            description=data.description,
            file=_media(info, data.media),
            transfer_function=data.transfer_function or "",
            dependency=_dump(data.dependency),
            organization=info.context.request.organization,
        ),
    )


def update_threed_model(info: Info, input: inputs.UpdateThreeDModelInput) -> types.ThreeDModel:
    """Partially update one of the caller's organization's 3D models."""
    data = input.to_pydantic()
    model = scoped_get(models.ThreeDModel, info, data.id)
    if data.name is not None:
        model.name = data.name
    if data.description is not None:
        model.description = data.description
    if data.media is not None:
        model.file = _media(info, data.media)
    if data.transfer_function is not None:
        model.transfer_function = data.transfer_function
    if data.dependency is not None:
        model.dependency = _dump(data.dependency)
    model.save()
    return cast("types.ThreeDModel", model)


def delete_threed_model(info: Info, input: inputs.DeleteThreeDModelInput) -> strawberry.ID:
    """Delete one of the caller's organization's 3D models."""
    data = input.to_pydantic()
    scoped_get(models.ThreeDModel, info, data.id).delete()
    return strawberry.ID(data.id)
