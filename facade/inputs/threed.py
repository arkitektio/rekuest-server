"""Inputs for spaces, agent placements and blok placements."""

import strawberry
from pydantic import BaseModel, Field
from strawberry.experimental import pydantic


class PlacementInputModel(BaseModel):
    role: str | None = Field(default=None, description="The role of the placement. This is used to identify the placement in the system.")
    affine_matrix: list[list[float]] | None = Field(default=None, description="The affine matrix for the placement. This is used to identify the placement in the system.")
    agent: str | None = Field(default=None, description="The agent ID for the placement. This is used to identify the agent in the system.")
    blok: str | None = Field(default=None, description="A specific blok that should be used to visualize the state of the placement.")


@pydantic.input(PlacementInputModel, description="The input for creating or updating a placement.")
class PlacementInput:
    role: str | None = None
    affine_matrix: list[list[float]] | None = None
    agent: strawberry.ID | None = None
    blok: strawberry.ID | None = None


class CreateSpaceInputModel(BaseModel):
    """Base model for creating a resolution.

    Attributes:
        name: Name of the resolution
        action_demands: List of action demands for the resolution
        state_demands: List of state demands for the resolution
        description: Description of the resolution
        url: URL associated with the resolution
    """

    name: str = Field(description="The name of the space. This is used to identify the space in the system.")
    placements: list[PlacementInputModel] | None = Field(default=None, description="The placements to create in the space. This is used to identify the placements in the system.")


@pydantic.input(
    CreateSpaceInputModel,
    description="The input for creating a space.",
)
class CreateSpaceInput:
    name: str
    placements: list[PlacementInput] | None = None


class UpdateSpaceInputModel(BaseModel):
    id: str = Field(description="The ID of the space to update.")
    name: str | None = Field(default=None, description="The new name of the space.")
    description: str | None = Field(default=None, description="The new description of the space.")


@pydantic.input(UpdateSpaceInputModel, description="The input for updating a space.")
class UpdateSpaceInput:
    id: strawberry.ID
    name: str | None = None
    description: str | None = None


class DeleteSpaceInputModel(BaseModel):
    id: str = Field(description="The ID of the space to delete.")


@pydantic.input(DeleteSpaceInputModel, description="The input for deleting a space.")
class DeleteSpaceInput:
    id: strawberry.ID


class CreatePlacementInputModel(PlacementInputModel):
    space: str = Field(description="The ID of the space to create the placement in.")
    materialized_blok: str | None = Field(default=None, description="A specific blok that should be used to visualize the state of the placement.")


class UpdatePlacementInputModel(PlacementInputModel):
    id: str = Field(description="The ID of the placement to update.")


@pydantic.input(CreatePlacementInputModel, description="The input for creating a placement.")
class CreatePlacementInput:
    space: str
    role: str | None = None
    affine_matrix: list[list[float]] | None = None
    agent: strawberry.ID | None = None
    materialized_blok: strawberry.ID | None = None


@pydantic.input(UpdatePlacementInputModel, description="The input for updating a placement.")
class UpdatePlacementInput:
    id: strawberry.ID
    role: str | None = None
    affine_matrix: list[list[float]] | None = None


class DeletePlacementInputModel(BaseModel):
    id: str = Field(description="The ID of the placement to delete.")


@pydantic.input(DeletePlacementInputModel, description="The input for deleting a placement.")
class DeletePlacementInput:
    id: strawberry.ID


class CreateSpacePlacementInputModel(BaseModel):
    space: str = Field(description="The space to place the blok in.")
    blok: str = Field(description="The materialized 3D blok to place.")
    name: str | None = Field(default=None, description="What the placement is called in its space.")
    affine_matrix: list[list[float]] | None = Field(default=None, description="Where the blok sits: a 4x4 affine matrix, row-major, from the blok's frame into the space's. Omitted is the identity.")


@pydantic.input(CreateSpacePlacementInputModel, description="Place a 3D blok in a space at an affine transform.")
class CreateSpacePlacementInput:
    space: strawberry.ID
    blok: strawberry.ID
    name: str | None = None
    affine_matrix: list[list[float]] | None = None


class UpdateSpacePlacementInputModel(BaseModel):
    id: str = Field(description="The blok placement to change.")
    name: str | None = Field(default=None, description="A new name.")
    affine_matrix: list[list[float]] | None = Field(default=None, description="A new 4x4 affine matrix, row-major.")


@pydantic.input(UpdateSpacePlacementInputModel, description="Move or rename a placed 3D blok.")
class UpdateSpacePlacementInput:
    id: strawberry.ID
    name: str | None = None
    affine_matrix: list[list[float]] | None = None


class DeleteSpacePlacementInputModel(BaseModel):
    id: str = Field(description="The blok placement to remove.")


@pydantic.input(DeleteSpacePlacementInputModel, description="Remove a placed 3D blok from its space.")
class DeleteSpacePlacementInput:
    id: strawberry.ID
