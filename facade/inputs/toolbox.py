"""Inputs for toolboxes and shortcuts."""

import strawberry
from pydantic import BaseModel, Field
from strawberry.experimental import pydantic

from facade import scalars
from facade.json_types import JSON


class CreateToolboxInputModel(BaseModel):
    name: str = Field(description="The name of the toolbox. This is used to identify the toolbox in the system.")
    description: str = Field(description="The description of the toolbox. This can described the toolbox and its purpose.")


@pydantic.input(CreateToolboxInputModel, description="The input for creating a toolbox.")
class CreateToolboxInput:
    name: str
    description: str | None = None


class DeleteToolboxInputModel(BaseModel):
    id: str = Field(description="The toolbox ID to delete. This is used to identify the toolbox in the system.")


@pydantic.input(DeleteToolboxInputModel, description="The input for deleting a toolbox.")
class DeleteToolboxInput:
    id: strawberry.ID


class CreateShortcutInputModel(BaseModel):
    name: str = Field(description="The name of the shortcut. This is used to identify the shortcut in the system.")
    description: str | None = Field(default=None, description="The description of the shortcut.This can described the shortcut and its purpose.")
    action: str = Field(description="The action ID to create a shortcut for")
    args: dict[str, JSON] = Field(description="The arguments to pre-pass to the shortcut. This is used to identify the shortcut in the system.")
    allow_quick: bool = Field(default=False, description="Whether to allow quick shortcuts. Quick shorts are shortcuts that can be autorun without scpeific assignment")
    use_returns: bool = Field(default=False, description="Whether when running the short the returns should be used further. Allows to create mini pipelines")
    bind_number: int | None = Field(default=None, description="The bind number of the shortcut. This is used to identify the shortcut in the system.")
    toolbox: str | None = Field(default=None, description="The toolbox ID to create the shortcut in. If not provided, the shortcut will be created in the default toolbox.")


@pydantic.input(CreateShortcutInputModel, description="The input for creating a shortcut.")
class CreateShortcutInput:
    action: strawberry.ID
    name: str
    toolbox: strawberry.ID | None = None
    description: str | None = None
    bind_number: int | None = None
    args: scalars.Args
    allow_quick: bool = False
    use_returns: bool = False


class DeleteShortcutInputModel(BaseModel):
    id: str = Field(description="The shortcut ID to delete. This is used to identify the shortcut in the system.")


@pydantic.input(DeleteShortcutInputModel, description="The input for deleting a shortcut.")
class DeleteShortcutInput:
    id: strawberry.ID
