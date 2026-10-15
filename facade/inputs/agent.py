"""Inputs for agent lifecycle controls (pin, bounce, kick, block, update)."""

from typing import Self

import strawberry
from pydantic import BaseModel, Field, model_validator
from strawberry.experimental import pydantic

from rekuest_core import enums

from rekuest_core.inputs.models import AssetInputModel, BlokImplementationInputModel, ImplementationInputModel, LockImplementationInputModel, StateImplementationInputModel


class PinInputModel(BaseModel):
    """Base model for pinning input data.

    Attributes:
        id: The unique identifier of the item to pin
        pin: Boolean flag indicating whether to pin or unpin
    """

    id: str = Field(description="The unique identifier of the item to pin.")
    pin: bool = Field(description="Boolean flag indicating whether to pin or unpin.")


@pydantic.input(PinInputModel, description="The input for pinning an model.")
class PinInput:
    id: strawberry.ID
    pin: bool


class BounceInputModel(BaseModel):
    """Base model for bouncing an agent.

    Attributes:
        agent: ID of the agent to bounce
    """

    agent: str = Field(description="The agent ID to bounce.")


@pydantic.input(BounceInputModel, description="The input for bouncing an agent.")
class BounceInput:
    agent: strawberry.ID


class KickInputModel(BaseModel):
    """Base model for bouncing an agent.

    Attributes:
        agent: ID of the agent to bounce
    """

    agent: str = Field(description="The agent ID to bounce.")
    reason: str | None = Field(default=None, description="The reason for kicking the agent.")


@pydantic.input(KickInputModel, description="The input for bouncing an agent.")
class KickInput:
    agent: strawberry.ID
    reason: str | None = None


class BlockInputModel(BaseModel):
    """Base model for bouncing an agent.

    Attributes:
        agent: ID of the agent to bounce
    """

    agent: str = Field(description="The agent ID to bounce.")
    reason: str | None = Field(default=None, description="The reason for kicking the agent.")


@pydantic.input(BlockInputModel, description="The input for bouncing an agent.")
class BlockInput:
    agent: strawberry.ID
    reason: str | None = None


class UnblockInputModel(BaseModel):
    """Base model for bouncing an agent.

    Attributes:
        agent: ID of the agent to bounce
    """

    agent: str = Field(description="The agent ID to unblock.")


@pydantic.input(UnblockInputModel, description="The input for bouncing an agent.")
class UnblockInput:
    agent: strawberry.ID


class UpdateAgentInputModel(BaseModel):
    """Base model for updating an agent.

    Attributes:
        id: The unique identifier of the agent to update
        name: The new name for the agent
    """

    id: str = Field(description="The ID of the agent to update.")
    name: str | None = Field(default=None, description="The new name for the agent.")


@pydantic.input(UpdateAgentInputModel, description="The input for updating an agent.")
class UpdateAgentInput:
    id: strawberry.ID
    name: str | None = None


class ImplementAgentInputModel(BaseModel):
    name: str | None = Field(default=None, description="The name of the agent. This is used to identify the agent in the system.")
    description: str | None = Field(default=None, description="What this agent is, in a sentence. Omitting it leaves whatever the agent already has, unlike `name`, which falls back to the client id.")
    states: list[StateImplementationInputModel] | None = Field(default=None, description="The states of the agent. This is used to specify the initial states of the agent")
    implementations: list[ImplementationInputModel] | None = Field(default=None, description="The implementations of the agent. This is used to specify the initial implementations of the agent")
    locks: list[LockImplementationInputModel] | None = Field(default=None, description="The locks of the agent. This is used to specify which resources the agent needs to run")
    bloks: list[BlokImplementationInputModel] | None = Field(default=None, description="The blocks of the agent. This is used to specify the initial blocks of the agent")
    assets: list[AssetInputModel] | None = Field(default=None, description="The files the agent brings with it, by name. The agent uploads the ones the server does not hold yet.")
    hash: str | None = Field(
        default=None,
        description="A unique hash of the agent definition. An agent can use this hash to check if its definition has changed and if it needs to update its implementations and states. This is used to optimize the update process by only updating the implementations and states that have changed.",
    )

    @model_validator(mode="after")
    def check_logos(self) -> Self:
        """An implementation's logo is a logo blok the agent declares."""
        logos = {blok.key for blok in self.bloks or [] if blok.context == enums.ComponentContext.LOGO}
        for implementation in self.implementations or []:
            if implementation.logo is not None and implementation.logo not in logos:
                raise ValueError(f"implementation {implementation.interface!r} is drawn by the logo {implementation.logo!r}, which is not a logo blok this agent declares")
        return self
