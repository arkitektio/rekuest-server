"""3D models: a media file, how an agent's state drives it, and which agent that is."""

import strawberry
from datalayer.models import MediaStore
from kante.types import Info

from facade import inputs, models, types
from facade.types.base import scoped_get


def _media(info: Info, media: str) -> MediaStore:
    try:
        return MediaStore.objects.get(id=media, organization=info.context.request.organization)
    except MediaStore.DoesNotExist:
        raise PermissionError(f"No media store {media} in your organization.") from None


def _dump(dependency: object) -> dict | None:
    if dependency is None:
        return None
    if hasattr(dependency, "to_pydantic"):
        dependency = dependency.to_pydantic()
    return dependency.model_dump(mode="json")


def create_threed_model(info: Info, input: inputs.CreateThreeDModelInput) -> types.ThreeDModel:
    """Create a 3D model in the caller's organization."""
    return models.ThreeDModel.objects.create(
        name=input.name,
        description=input.description,
        file=_media(info, input.media),
        transfer_function=input.transfer_function or "",
        dependency=_dump(input.dependency),
        organization=info.context.request.organization,
    )


def update_threed_model(info: Info, input: inputs.UpdateThreeDModelInput) -> types.ThreeDModel:
    """Partially update one of the caller's organization's 3D models."""
    model = scoped_get(models.ThreeDModel, info, input.id)
    if input.name is not None:
        model.name = input.name
    if input.description is not None:
        model.description = input.description
    if input.media is not None:
        model.file = _media(info, input.media)
    if input.transfer_function is not None:
        model.transfer_function = input.transfer_function
    if input.dependency is not None:
        model.dependency = _dump(input.dependency)
    model.save()
    return model


def delete_threed_model(info: Info, input: inputs.DeleteThreeDModelInput) -> strawberry.ID:
    """Delete one of the caller's organization's 3D models."""
    scoped_get(models.ThreeDModel, info, input.id).delete()
    return input.id
