"""Inputs for dependency mapping and resolution."""

from typing import Annotated

import strawberry
from pydantic import BaseModel, Field
from strawberry.experimental import pydantic


class MappedAgentInputModel(BaseModel):
    key: str = Field(description="The key of the agent to map. This is used to identify the agent in the system.")
    agent: str = Field(description="The agent ID to map the actions to. This is used to identify the agent in the system.")
    # None, not []: the dump drops it, so an assign that pins nothing below reads as it always did.
    dependencies: list["ResolvedDependencyInputModel"] | None = Field(
        default=None,
        description="Overwrites one level down: for the dependencies of the implementations bound on this agent. A dependency key repeats across levels, so an overwrite only ever applies at the level it is given.",
    )


class ResolvedDependencyInputModel(BaseModel):
    key: str = Field(description="The key of the dependency to map. This is used to identify the dependency in the system.")
    mapped_agents: list[MappedAgentInputModel] = Field(description="The list of mapped agents to map to implementations in agents. This is used to identify the mapped agents in the system.")
    auto_resolve: bool = Field(
        default=False,
        description="Whether this dependency should be automatically resolved by the system. If true, the system will attempt to find a agent that can resolve this dependency and assign it to the action when the action is assigned. This is used to enable automatic resolution of dependencies without requiring the user to specify a specific agent for the dependency.",
    )


MappedAgentInputModel.model_rebuild()


@pydantic.input(
    MappedAgentInputModel,
    description="The input for mapping actions to implementations in a agent.",
)
class MappedAgentInput:
    key: str
    agent: strawberry.ID
    dependencies: list[Annotated["ResolvedDependencyInput", strawberry.lazy("facade.inputs.dependency")]] | None = None


@pydantic.input(
    ResolvedDependencyInputModel,
    description="The input for mapping dependencies to implementations in a agent.",
)
class ResolvedDependencyInput:
    key: str
    mapped_agents: list[MappedAgentInput]
    auto_resolve: bool = False


class DependencyTreeInputModel(BaseModel):
    implementation: str = Field(description="The implementation an assign would target.")
    dependencies: list[ResolvedDependencyInputModel] | None = Field(default=None, description="The overwrites the assign would carry.")


@pydantic.input(
    DependencyTreeInputModel,
    description="An assign's dependencies, to resolve without assigning.",
)
class DependencyTreeInput:
    implementation: strawberry.ID
    dependencies: list[ResolvedDependencyInput] | None = None


@strawberry.input
class AutoResolveInput:
    implementation: strawberry.ID


class UpdateResolutionInputModel(BaseModel):
    """Base model for creating a resolution.

    Attributes:
        name: Name of the resolution
        action_demands: List of action demands for the resolution
        state_demands: List of state demands for the resolution
        description: Description of the resolution
        url: URL associated with the resolution
    """

    id: str = Field(description="The ID of the resolution. This is used to identify the resolution in the system.")
    name: str = Field(description="The name of the resolution. This is used to identify the resolution in the system.")
    resolved_dependencies: list[ResolvedDependencyInputModel] | None = Field(
        default=None,
        description="The resolved dependencies of the resolution. All other fields will be replaced.",
    )


@pydantic.input(
    UpdateResolutionInputModel,
    description="The input for creating a resolution.",
)
class UpdateResolutionInput:
    id: strawberry.ID
    name: str
    resolved_dependencies: list[ResolvedDependencyInput] | None = None


class CreateResolutionInputModel(BaseModel):
    """Base model for creating a resolution.

    Attributes:
        name: Name of the resolution
        action_demands: List of action demands for the resolution
        state_demands: List of state demands for the resolution
        description: Description of the resolution
        url: URL associated with the resolution
    """

    key: str = Field(description="The key of the resolution. This is used to identify the resolution in the system.")
    implementation: str = Field(description="The implementation ID of the resolution. This is used to identify the resolution in the system.")
    name: str = Field(description="The name of the resolution. This is used to identify the resolution in the system.")
    resolved_dependencies: list[ResolvedDependencyInputModel] | None = Field(
        default=None,
        description="The resolved dependencies of the resolution. This is used to identify the resolution in the system.",
    )


@pydantic.input(
    CreateResolutionInputModel,
    description="The input for creating a resolution.",
)
class CreateResolutionInput:
    key: str
    implementation: strawberry.ID
    name: str
    resolved_dependencies: list[ResolvedDependencyInput] | None = None


class DeleteResolutionInputModel(BaseModel):
    """Base model for deleting a resolution.

    Attributes:
        id: The unique identifier of the resolution to delete
    """

    id: str = Field(description="The ID of the resolution to delete.")


@pydantic.input(
    DeleteResolutionInputModel,
    description="The input for deleting a resolution.",
)
class DeleteResolutionInput:
    id: strawberry.ID
