"""Inputs for bloks and materialized bloks."""

import strawberry
from pydantic import BaseModel, Field, field_validator, model_validator
from strawberry.experimental import pydantic
from typing_extensions import Self

from facade import scalars
from facade.inputs.dependency import MappedAgentInput, MappedAgentInputModel
from facade.json_types import JSON
from rekuest_core import enums as renums
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
    source: str | None = Field(default=None, description="The blok as BSX, which the server parses into `components`: send one of the two.")
    demo_state: dict[str, JSON] | None = Field(
        default=None,
        description="The initial state of the blok. This is used to set the initial state of the blok when it is materialized.",
    )
    context: renums.ComponentContext = Field(default=renums.ComponentContext.HTML, description="The renderer for the blok's root: HTML, or SCENE for a full 3D view.")
    for_model: str | None = Field(default=None, description="The model ('sha256:...') this blok decorates. HTML bloks only.")
    for_node: str | None = Field(default=None, description="The node of `for_model` this blok attaches to; the model's root when absent.")
    origin: str | None = Field(default=None, max_length=2000, description="Where the blok is installed from (kabinet's `RepoBlok.origin`). A blok of the same origin is updated in place instead of matched by name.")
    origin_hash: str | None = Field(default=None, max_length=100, description="The hash its origin gives what it draws, kept to tell when the origin has an update.")

    @field_validator("context", mode="before")
    @classmethod
    def _html_when_absent(cls, context: object) -> object:
        """A blok that names no context is an html one: the argument is optional on the wire."""
        return renums.ComponentContext.HTML if context is None else context

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        """Sent one way; the component tree coherent with the declared dependencies and demo state.

        A source is checked once ``createBlok`` has had takt parse it into components."""
        rimodels.check_blok_sent_once(self.components, self.source, required=False)
        rimodels.check_blok_manifest(self.components, {dep.key for dep in self.dependencies or []}, None if self.demo_state is None else set(self.demo_state))
        rimodels.check_blok_target(self.context, self.for_model, self.for_node)
        return self


@pydantic.input(CreateBlokInputModel, description="The input for creating a blok.")
class CreateBlokInput:
    name: str
    dependencies: list[ritypes.AgentDependencyInput] | None = None
    description: str | None = None
    catalog: str | None = None
    components: list[ritypes.ComponentNodeInput] | None = None
    source: str | None = None
    demo_state: scalars.Args | None = None
    # Nullable, not defaulted: a client generated from the schema must not have to pass it.
    context: renums.ComponentContext | None = None
    for_model: str | None = None
    for_node: str | None = None
    origin: str | None = None
    origin_hash: str | None = None


class UpdateBlokInputModel(BaseModel):
    """Partial update of a Blok. ``None`` leaves a field unchanged; list fields replace wholesale."""

    id: str = Field(description="The ID of the blok to update.")
    name: str | None = Field(default=None, description="The name of the blok, used for identification in the system.")
    description: str | None = Field(default=None, description="The description of the blok and its purpose.")
    components: list[rimodels.ComponentNodeInputModel] | None = Field(
        default=None,
        description="The full component tree of the blok. Replaces the existing tree when provided.",
    )
    source: str | None = Field(default=None, description="The blok as BSX, which the server parses into the tree it replaces the existing one with: send it or `components`, not both.")

    @model_validator(mode="after")
    def check_sent_once(self) -> Self:
        """A new tree comes as components or as source, not both."""
        rimodels.check_blok_sent_once(self.components, self.source, required=False)
        return self
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


EDIT_CONTENT = "Use editBlok: it checks the write is made on the head you read, keeps a message with the revision, and has a dry run. updateBlok keeps name and description."


@pydantic.input(UpdateBlokInputModel, description="The input for updating a blok. Omitted (null) fields are left unchanged; list fields replace wholesale.")
class UpdateBlokInput:
    id: strawberry.ID
    name: str | None = None
    description: str | None = None
    components: list[ritypes.ComponentNodeInput] | None = strawberry.field(default=None, deprecation_reason=EDIT_CONTENT)
    source: str | None = strawberry.field(default=None, deprecation_reason=EDIT_CONTENT)
    demo_state: scalars.Args | None = strawberry.field(default=None, deprecation_reason=EDIT_CONTENT)
    catalog: str | None = strawberry.field(default=None, deprecation_reason=EDIT_CONTENT)
    dependencies: list[ritypes.AgentDependencyInput] | None = strawberry.field(default=None, deprecation_reason=EDIT_CONTENT)


@strawberry.input(description="The input for updating a blok.")
class DeleteBlokInput:
    id: strawberry.ID = strawberry.field(description="The blok ID to delete. This is used to identify the blok in the system.")


@strawberry.input(description="Copy a blok an app provides into a custom blok of the caller's organization, to edit it or bind other agents to it.")
class ForkBlokInput:
    id: strawberry.ID = strawberry.field(description="The provided blok to fork.")
    name: str | None = strawberry.field(default=None, description="The custom blok's name. Defaults to the provided blok's.")


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


class EditBlokInputModel(BaseModel):
    """An edit of a blok, made on it or through a materialization of it: the parts given replace the head's."""

    blok: str | None = Field(default=None, description="The blok to edit, as an editor of the blok itself does. Send it or `materialized_blok`, not both.")
    materialized_blok: str | None = Field(default=None, description="The materialization the edit is made through, which also takes `agent_mappings`. Send it or `blok`, not both.")
    base: str | None = Field(default=None, description="The revision the edit starts from. It must still be the blok's head, or the edit is refused. Required for a custom blok; a provided blok has no revisions and is forked.")
    components: list[rimodels.ComponentNodeInputModel] | None = Field(default=None, description="The full component tree. Kept when omitted.")
    source: str | None = Field(default=None, description="The full component tree as BSX, which the server parses: send it or `components`, not both.")
    demo_state: dict[str, JSON] | None = Field(default=None, description="The full local state. Kept when omitted.")
    catalog: str | None = Field(default=None, description="Name of the UI catalog (in the caller's organization) the blok renders against. Kept when omitted.")
    dependencies: list[rimodels.AgentDependencyInputModel] | None = Field(default=None, description="The full list of agent dependencies. Kept when omitted; keys not listed are removed.")
    agent_mappings: list[MappedAgentInputModel] | None = Field(default=None, description="Bindings on `materialized_blok`, by dependency key, over the ones it has. Bindings of keys the edit removes go. Refused with `blok`: a blok binds no agents, its materializations do.")
    message: str | None = Field(default=None, description="What changed and why, in a sentence; kept with the revision.")
    task: str | None = Field(default=None, description="The task making the edit (one the caller's agent runs), kept with the revision.")
    dry_run: bool | None = Field(default=None, description="Check everything and return the findings, keeping nothing. Off when omitted.")

    @model_validator(mode="after")
    def check_sent_once(self) -> Self:
        rimodels.check_blok_sent_once(self.components, self.source, required=False)
        if (self.blok is None) == (self.materialized_blok is None):
            raise ValueError("Edit a blok, or a materialization of it: send `blok` or `materialized_blok`, one of the two.")
        if self.blok is not None and self.agent_mappings is not None:
            raise ValueError("A blok binds no agents, its materializations do: send `materialized_blok` with `agent_mappings`.")
        return self


@pydantic.input(EditBlokInputModel, description="Edit a blok, on it or through a materialization of it (and its bindings there), as one new revision.")
class EditBlokInput:
    blok: strawberry.ID | None = None
    materialized_blok: strawberry.ID | None = None
    base: strawberry.ID | None = None
    components: list[ritypes.ComponentNodeInput] | None = None
    source: str | None = None
    demo_state: scalars.Args | None = None
    catalog: str | None = None
    dependencies: list[ritypes.AgentDependencyInput] | None = None
    agent_mappings: list[MappedAgentInput] | None = None
    message: str | None = None
    task: strawberry.ID | None = None
    dry_run: bool | None = None


@strawberry.input(description="Make an earlier revision's content a blok's head again.")
class RevertBlokInput:
    blok: strawberry.ID = strawberry.field(description="The custom blok.")
    to: strawberry.ID = strawberry.field(description="The revision whose content comes back.")
    base: strawberry.ID = strawberry.field(description="The revision the caller saw as the head; refused when the head moved since.")
    message: str | None = strawberry.field(default=None, description="Why, in a sentence. Defaults to 'Back to revision <n>'.")
    task: strawberry.ID | None = strawberry.field(default=None, description="The task making the change (one the caller's agent runs).")


@strawberry.input(description="Keep a materialization at one revision of its blok, or let it follow the head.")
class PinMaterializedBlokInput:
    id: strawberry.ID = strawberry.field(description="The materialization.")
    revision: strawberry.ID | None = strawberry.field(default=None, description="The revision to stay at; null follows the head again.")
