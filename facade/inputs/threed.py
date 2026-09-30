"""Inputs for 3D models, spaces and placements."""

import strawberry
from datalayer import scalars as dscalars
from pydantic import BaseModel, Field
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs import types as ritypes
from strawberry.experimental import pydantic


class CreateThreeDModelInputModel(BaseModel):
    name: str = Field(description="The name of the 3D model.")
    description: str | None = Field(default=None, description="A description of the 3D model.")
    media: str = Field(description="The media store file for the 3D model.")
    transfer_function: str | None = Field(default=None, description="How the agent's state maps onto the model's properties.")
    dependency: rimodels.AgentDependencyInputModel | None = Field(default=None, description="The agent this model shows: whose state the transfer function reads.")


@pydantic.input(CreateThreeDModelInputModel, description="The input for creating a 3D model.")
class CreateThreeDModelInput:
    name: str
    description: str | None = None
    media: dscalars.MediaLike
    transfer_function: str | None = None
    dependency: ritypes.AgentDependencyInput | None = None


class UpdateThreeDModelInputModel(BaseModel):
    id: str = Field(description="The ID of the 3D model to update.")
    name: str | None = Field(default=None, description="The new name of the 3D model.")
    description: str | None = Field(default=None, description="The new description of the 3D model.")
    media: str | None = Field(default=None, description="The new media store file ID for the 3D model.")
    transfer_function: str | None = Field(default=None, description="How the agent's state maps onto the model's properties.")
    dependency: rimodels.AgentDependencyInputModel | None = Field(default=None, description="The agent this model shows.")


@pydantic.input(UpdateThreeDModelInputModel, description="The input for updating a 3D model.")
class UpdateThreeDModelInput:
    id: strawberry.ID
    name: str | None = None
    description: str | None = None
    media: strawberry.ID | None = None
    transfer_function: str | None = None
    dependency: ritypes.AgentDependencyInput | None = None


class PlacementInputModel(BaseModel):
    role: str | None = Field(default=None, description="The role of the placement. This is used to identify the placement in the system.")
    affine_matrix: list[list[float]] | None = Field(default=None, description="The affine matrix for the placement. This is used to identify the placement in the system.")
    model: str | None = Field(default=None, description="The 3D model ID for the placement. This is used to identify the 3D model in the system.")
    agent: str | None = Field(default=None, description="The agent ID for the placement. This is used to identify the agent in the system.")
    blok: str | None = Field(default=None, description="A specific blok that should be used to visualize the state of the placement.")


class DeleteThreeDModelInputModel(BaseModel):
    id: str = Field(description="The ID of the 3D model to delete.")


@pydantic.input(PlacementInputModel, description="The input for creating or updating a placement.")
class PlacementInput:
    role: str | None = None
    affine_matrix: list[list[float]] | None = None
    model: strawberry.ID | None = None
    agent: strawberry.ID | None = None
    blok: strawberry.ID | None = None


@pydantic.input(DeleteThreeDModelInputModel, description="The input for deleting a 3D model.")
class DeleteThreeDModelInput:
    id: strawberry.ID


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
    model: strawberry.ID | None = None
    agent: strawberry.ID | None = None
    materialized_blok: strawberry.ID | None = None


@pydantic.input(UpdatePlacementInputModel, description="The input for updating a placement.")
class UpdatePlacementInput:
    id: strawberry.ID
    role: str | None = None
    affine_matrix: list[list[float]] | None = None
    model: strawberry.ID | None = None


class DeletePlacementInputModel(BaseModel):
    id: str = Field(description="The ID of the placement to delete.")


@pydantic.input(DeletePlacementInputModel, description="The input for deleting a placement.")
class DeletePlacementInput:
    id: strawberry.ID
