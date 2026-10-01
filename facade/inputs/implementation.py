"""Inputs for implementations and action/schema/port demands."""


import strawberry
from pydantic import BaseModel, Field
from rekuest_core import scalars as rscalars
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs import types as ritypes
from strawberry.experimental import pydantic

from facade import enums


@strawberry.input(description="The input for creating a port demand.")
class PortDemandInput:
    kind: enums.DemandKind = strawberry.field(
        description="The kind of the demand. You can ask for args or returns",
    )
    matches: list[ritypes.PortMatchInput] | None = strawberry.field(
        default=None,
        description="The matches of the demand. ",
    )
    force_length: int | None = strawberry.field(
        default=None,
        description="Require that the action has a specific number of ports. This is used to identify the demand in the system.",
    )
    force_non_nullable_length: int | None = strawberry.field(
        default=None,
        description="Require that the action has a specific number of non-nullable ports. This is used to identify the demand in the system.",
    )
    force_structure_length: int | None = strawberry.field(
        default=None,
        description="Require that the action has a specific number of structure ports. This is used to identify the demand in the system.",
    )


class CreateHigherOrderImplementationInputModel(BaseModel):
    """A wrapper to deploy onto the agent of the implementation it wraps."""

    lower: str = Field(description="The implementation to wrap; its agent hosts the wrapper.")
    interface: str = Field(description="The wrapper's interface, unique on that agent (e.g. 'flow:123').")
    definition: rimodels.DefinitionInputModel = Field(description="The wrapper's typed contract, derived by the caller.")
    config: dict | None = Field(default=None, description="Projection config: bound params + arg/dependency/return maps (see Implementation.higher_order_config).")
    dependencies: list[rimodels.AgentDependencyInputModel] | None = Field(default=None, description="Dependencies the wrapper declares, for a dependency_map sourcing 'from: caller'.")


@pydantic.input(
    CreateHigherOrderImplementationInputModel,
    description="Deploy a higher-order implementation: a wrapper onto the agent of the implementation it wraps.",
)
class CreateHigherOrderImplementationInput:
    lower: strawberry.ID
    interface: str
    definition: ritypes.DefinitionInput
    config: rscalars.AnyDefault | None = None
    dependencies: list[ritypes.AgentDependencyInput] | None = None


class DeleteImplementationInputModel(BaseModel):
    """Base model for deleting an implementation.

    Attributes:
        implementation: ID of the implementation to delete
    """

    implementation: str = Field(description="The implementation ID to delete. This is used to identify the implementation in the system.")


@pydantic.input(
    DeleteImplementationInputModel,
    description="The input for deleting a implementation.",
)
class DeleteImplementationInput:
    implementation: strawberry.ID
