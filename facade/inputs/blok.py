"""Inputs for bloks and materialized bloks."""

import strawberry
from pydantic import BaseModel, Field, model_validator
from strawberry.experimental import pydantic
from typing_extensions import Self

from facade import scalars
from facade.inputs.dependency import MappedAgentInput
from facade.json_types import JSON
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs import types as ritypes


@strawberry.input(description="Input for updating a dashboard. This is used to update the properties of a dashboard, such as its name, associated bloks, or organization.")
class DeleteMaterializedBlokInput:
    id: strawberry.ID = strawberry.field(description="The ID of the materialized blok to delete.")


@strawberry.input(description="Input for updating a materialized blok. This is used to update the properties of a materialized blok, such as its associated agent mappings.")
class UpdateMaterializedBlokInput:
    id: strawberry.ID = strawberry.field(description="The ID of the materialized blok to update.")
    agent_mappings: list[MappedAgentInput] | None = strawberry.field(
        default=None,
        description="The list of mapped agents to update the materialized blok with. This is used to update the agent mappings of the materialized blok.",
    )


class CreateBlokInputModel(BaseModel):
    """Base model for creating a Blok, which is a reusable UI component with optional agent interactions.

    Attributes:
        name: The name of the Blok, used for identification in the system.
        dependencies: An optional list of agent dependencies declared in the Blok manifest, used to identify the Blok in the system.
        description: An optional description of the Blok and its purpose.
        catalog: An optional universal ID for the Blok.
        uri: The URI of the Blok, used to link to it in the system.
        components: An optional list of component nodes defining the Blok's visual structure and behavior.
    """

    name: str = Field(description="The name of the Blok, used for identification in the system.")
    dependencies: list[rimodels.AgentDependencyInputModel] | None = Field(
        default=None,
        description="The dependencies of the blok. This is used to identify the blok in the system.",
    )
    description: str | None = Field(
        default=None,
        description="The description of the blok. This can described the blok and its purpose.",
    )
    catalog: str | None = Field(
        default=None,
        description="The universal id",
    )
    components: list[rimodels.ComponentNodeInputModel] | None = Field(
        default=None,
        description="The schema of the blok. This can be used to validate the blok input and output.",
    )
    demo_state: dict[str, JSON] | None = Field(
        default=None,
        description="The initial state of the blok. This is used to set the initial state of the blok when it is materialized.",
    )

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        """The component tree is coherent with the declared dependencies and demo state."""
        rimodels.check_blok_manifest(self.components, {dep.key for dep in self.dependencies or []}, None if self.demo_state is None else set(self.demo_state))
        return self


@pydantic.input(CreateBlokInputModel, description="The input for creating a blok.")
class CreateBlokInput:
    name: str
    dependencies: list[ritypes.AgentDependencyInput] | None = None
    description: str | None = None
    catalog: str | None = None
    components: list[ritypes.ComponentNodeInput] | None = None
    demo_state: scalars.Args | None = None


class UpdateBlokInputModel(BaseModel):
    """Partial update of a Blok. ``None`` leaves a field unchanged; list fields replace wholesale."""

    id: str = Field(description="The ID of the blok to update.")
    name: str | None = Field(default=None, description="The name of the blok, used for identification in the system.")
    description: str | None = Field(default=None, description="The description of the blok and its purpose.")
    components: list[rimodels.ComponentNodeInputModel] | None = Field(
        default=None,
        description="The full component tree of the blok. Replaces the existing tree when provided.",
    )
    demo_state: dict[str, JSON] | None = Field(
        default=None,
        description="The demo state used to preview this blok. Replaces the existing demo state when provided.",
    )
    catalog: str | None = Field(
        default=None,
        description="Name of the UI catalog (in the caller's organization) this blok renders against. Created if missing.",
    )
    dependencies: list[rimodels.AgentDependencyInputModel] | None = Field(
        default=None,
        description="The full list of agent dependencies. When provided it replaces the existing set: keys not listed are deleted and their agent mappings on materialized bloks lose their dependency reference.",
    )


@pydantic.input(UpdateBlokInputModel, description="The input for updating a blok. Omitted (null) fields are left unchanged; list fields replace wholesale.")
class UpdateBlokInput:
    id: strawberry.ID
    name: str | None = None
    description: str | None = None
    components: list[ritypes.ComponentNodeInput] | None = None
    demo_state: scalars.Args | None = None
    catalog: str | None = None
    dependencies: list[ritypes.AgentDependencyInput] | None = None


@strawberry.input(description="The input for updating a blok.")
class DeleteBlokInput:
    id: strawberry.ID = strawberry.field(description="The blok ID to delete. This is used to identify the blok in the system.")


@strawberry.input(description="The input for updating a blok.")
class BlokAgentMappingInput:
    agent: strawberry.ID
    key: str


@strawberry.input(description="The input for creating a blok.")
class MaterializeBlokInput:
    blok: strawberry.ID
    dashboard: strawberry.ID | None = strawberry.field(
        default=None,
        description="The dashboard ID to materialize the blok in. If not provided, the blok will be materialized in the default dashboard.",
    )
    agent_mappings: list[BlokAgentMappingInput] | None = strawberry.field(
        default=None,
        description="The agent mappings for the blok. This is used to map the blok dependencies to agents in the system.",
    )
    name: str | None = strawberry.field(default=None, description="Display name of this materialization. Defaults to the blok's name.")
    description: str | None = strawberry.field(default=None, description="Description of this materialization. Defaults to the blok's description.")
