"""Inputs for tasks and the postman lifecycle controls."""

import datetime
from typing import Any

import strawberry
from pydantic import BaseModel, Field
from rekuest_core import scalars as rscalars
from strawberry.experimental import pydantic

from facade import enums, scalars
from facade.inputs.dependency import ResolvedDependencyInput, ResolvedDependencyInputModel


class HookInputModel(BaseModel):
    """Base model for hook input data.

    Attributes:
        kind: The type/kind of hook to be executed
        hash: Hash identifier for the hook action
    """

    kind: enums.HookKind = Field(description="The kind of the hook. This is used to identify the hook in the system.")
    hash: str = Field(description="The hash of the hook. This is used to identify the hook in the system.")


@pydantic.input(
    HookInputModel,
    description="A hook is a function that is called when a action has reached a specific lifecycle point. Hooks are jsut actions that take a task as input and return a value.",
)
class HookInput:
    kind: enums.HookKind
    hash: rscalars.ActionHash


class AssignInputModel(BaseModel):
    """Base model for assigning arguments to an action.

    Attributes:
        action: Optional action ID to assign to
        implementation: Optional implementation ID for direct assignment
        agent: Optional agent ID for direct assignment
        action_hash: Optional hash of the action for identification
        interface: Optional interface of the implementation
        hooks: Optional list of hooks for the task
        args: Dictionary of arguments/ports and values
        reference: Unique reference identifier for the task
        parent: Optional parent ID of the task
        step: Whether the task should step to breakpoints
    """

    action: str | None = Field(default=None, description="The action ID to assign to")
    dependency: str | None = Field(default=None, description="The dependency key.method to assign when running inside a resolved task")
    resolution: str | None = Field(
        default=None,
        description="The resolution ID to assign to when assining to a implementation with dependencies",
    )  # if assining to a implementation with dependencies
    implementation: str | None = Field(
        default=None,
        description="The implementation ID to assign to when directly assingint to a implementation",
    )
    agent: str | None = Field(
        default=None,
        description="The agent ID to assign to when directly assingint to a implementation",
    )
    action_hash: str | None = Field(
        default=None,
        description="The hash of the action. This is used to identify the action in the system.",
    )
    method: str | None = Field(default=None, description="The method key to assign when running inside a resolved task")
    interface: str | None = Field(
        default=None,
        description="The interface of the implementation. Only ussable if you also set agent",
    )
    hooks: list[HookInputModel] | None = Field(
        default=None,
        description="The hooks of the task. This is used to identify the task in the system.",
    )
    args: dict[str, Any] = Field(description="The args of the task. Its a dictionary of ports and values")
    reference: str | None = Field(
        default=None,
        description="The reference of the task. This is used to identify the task in the system.",
    )
    parent: str | None = Field(
        default=None,
        description="The parent ID of the task. This is used to identify the task in the system.",
    )
    capture: bool | None = Field(default=None, description="Whether to capture the task.")
    dependencies: list[ResolvedDependencyInputModel] | None = Field(
        default=None,
        description="The dependencies of the task. This maps dependency keys to implementation IDs.",
    )
    step: bool | None = Field(default=None, description="Whether the task should step. Ie. go to the next breakpoint")
    not_before: datetime.datetime | None = Field(
        default=None,
        description="Hold the task back until then: it is persisted now but only dispatched once due. A time in the past (or none) dispatches immediately.",
    )


@pydantic.input(AssignInputModel, description="The input for assigning args to a action. A GraphQL assign is a ROOT by definition — children are created only over the agent socket (AssignRequest, where parent is mandatory) and by server-internal paths like init hooks, so parent/dependency/method are deliberately absent here.")
class AssignInput:
    action: strawberry.ID | None = None
    implementation: strawberry.ID | None = None
    agent: strawberry.ID | None = None
    action_hash: rscalars.ActionHash | None = None
    interface: str | None = None
    resolution: strawberry.ID | None = None
    hooks: list[HookInput] | None = strawberry.field(default_factory=list)
    capture: bool = False
    args: scalars.Args
    reference: str | None = None
    step: bool | None = False
    dependencies: list[ResolvedDependencyInput] | None = None
    not_before: datetime.datetime | None = None


class CancelInputModel(BaseModel):
    """Base model for canceling a task.

    Attributes:
        task: ID of the task to cancel
    """

    task: str = Field(description="The task ID to cancel")


@pydantic.input(CancelInputModel, description="The input for canceling a task.")
class CancelInput:
    task: strawberry.ID


class PauseInputModel(BaseModel):
    """Base model for pausing a task.

    Attributes:
        task: ID of the task to pause
    """

    task: str = Field(description="The task ID to pause")


@pydantic.input(PauseInputModel, description="The input for pausing a task.")
class PauseInput:
    task: strawberry.ID


class CollectInputModel(BaseModel):
    """Base model for collecting shelved items from drawers.

    Attributes:
        drawers: List of drawer IDs to collect from
    """

    drawers: list[str] = Field(description="The drawer ID to collect")


@pydantic.input(
    CollectInputModel,
    description="The input for collecting a shelved item in a drawer.",
)
class CollectInput:
    drawers: list[strawberry.ID]


class ResumeInputModel(BaseModel):
    """Base model for resuming a paused task.

    Attributes:
        task: ID of the task to resume
        step: resume only to the next breakpoint (the equivalent of the old step instruction)
    """

    task: str = Field(description="The task ID to resume")
    step: bool = Field(default=False, description="Resume only until the next breakpoint instead of running on freely.")


@pydantic.input(ResumeInputModel, description="The input for resuming a task.")
class ResumeInput:
    task: strawberry.ID
    step: bool = False


class InterruptInputModel(BaseModel):
    """Base model for interrupting a task.

    Attributes:
        task: ID of the task to interrupt
    """

    task: str = Field(description="The task ID to interrupt")


@pydantic.input(InterruptInputModel, description="The input for interrupting a task.")
class InterruptInput:
    task: strawberry.ID
