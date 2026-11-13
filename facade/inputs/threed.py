"""Inputs for keeping where agents stand in a space (a room)."""

import strawberry
from pydantic import BaseModel, Field
from strawberry.experimental import pydantic


class SavePlacementInputModel(BaseModel):
    room: str = Field(description="The space's reference: a blok's `room` (a URI such as `room:helsinki-lab`, or `app/<identifier>/<key>`). The space is made when none has it yet.")
    agent: str = Field(description="The agent whose pose is kept.")
    role: str = Field(description="The dependency key it was placed under (`robot`).")
    affine_matrix: list[list[float]] = Field(description="Where it stands: a 4x4 affine matrix, row-major, in meters with Y up, from the agent's model into the space.")


@pydantic.input(SavePlacementInputModel, description="Keep where an agent stands in a space, for everyone.")
class SavePlacementInput:
    room: str
    agent: strawberry.ID
    role: str
    affine_matrix: list[list[float]]


class ResetPlacementInputModel(BaseModel):
    room: str = Field(description="The space's reference.")
    agent: str = Field(description="The agent whose kept pose is forgotten.")


@pydantic.input(ResetPlacementInputModel, description="Forget where an agent was left in a space, so the blok that places it decides again.")
class ResetPlacementInput:
    room: str
    agent: strawberry.ID
