"""Inputs for tasks and the postman lifecycle controls."""

import datetime
from typing import Self

import strawberry
from pydantic import BaseModel, ConfigDict, Field, model_validator
from strawberry.experimental import pydantic

from facade import enums, scalars
from facade.inputs.dependency import ResolvedDependencyInput, ResolvedDependencyInputModel
from facade.json_types import JSON
from rekuest_core import enums as rcenums
from rekuest_core import scalars as rscalars


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


class StateRequirementInputModel(BaseModel):
    """What the agent's state must hold for a task to run.

    The agent tests it after taking the task's locks and the locks the state requires;
    on a mismatch the task ends FAILED (code STATE_PRECONDITION) and nothing of it runs.
    The server checks only that the state and the path's first member are declared,
    and sends requirements only to an agent that says it tests them.
    """

    state: str = Field(min_length=1, description="The state's interface on the agent.")
    path: str = Field(pattern=r"^/", description="An RFC 6901 pointer into the state's value, e.g. '/busy' or '/stage/position/0'.")
    operator: rcenums.DescriptorOperator = Field(description="How the value at `path` is compared with `value`.")
    value: JSON = Field(default=None, description="What the value is compared with: IN/NOT_IN take a list, LTE/GTE a number, EXISTS true or false.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_operator_value(self) -> Self:
        """Operator and value agree, as for a port's requires."""
        operator, value, where = self.operator, self.value, f"{self.state}{self.path}"
        if operator in (rcenums.DescriptorOperator.IN, rcenums.DescriptorOperator.NOT_IN) and not isinstance(value, list):
            raise ValueError(f"state requirement {where!r}: {operator.value} needs a list value")
        if operator in (rcenums.DescriptorOperator.LTE, rcenums.DescriptorOperator.GTE) and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"state requirement {where!r}: {operator.value} needs a numeric value")
        if operator == rcenums.DescriptorOperator.EXISTS and value is not None and not isinstance(value, bool):
            raise ValueError(f"state requirement {where!r}: EXISTS takes no value or a boolean")
        return self


@pydantic.input(
    StateRequirementInputModel,
    description="What the agent's state must hold for the task to run: the value at `path` (an RFC 6901 pointer into the state `state`) compared with `value` by `operator`. The agent tests it with the task's locks held; on a mismatch the task ends FAILED with code STATE_PRECONDITION and nothing of it runs.",
)
class StateRequirementInput:
    state: str
    path: str
    operator: rcenums.DescriptorOperator
    value: rscalars.Arg | None = None


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
    args: dict[str, JSON] = Field(description="The args of the task. Its a dictionary of ports and values")
    reference: str | None = Field(
        default=None,
        description="The reference of the task. This is used to identify the task in the system.",
    )
    parent: str | None = Field(
        default=None,
        description="The parent ID of the task. This is used to identify the task in the system.",
    )
    parent_step: int | None = Field(
        default=None,
        description="The parent's step this child takes (agent-socket assigns of numbering agents). Idempotent on (parent, parent_step). Not exposed on the GraphQL assign, which creates roots only.",
    )
    call_key: str | None = Field(
        default=None,
        description="What the parent calls this child. Idempotent on (parent, call_key), checked before (parent, parent_step). Agent-socket assigns only.",
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
    policy: enums.TaskPolicy | None = Field(
        default=None,
        description="Whether the work the agents ask for under this task waits for a person: ASK (the default) holds every child an agent assigns until someone approves it, AUTO runs it without asking (auto-mode). Honoured on a root only: a child always takes its parent's.",
    )
    state_requires: list[StateRequirementInputModel] | None = Field(
        default=None,
        description="What the agent's state must hold for the task to run. Refused unless the agent says it tests them, and for a state or path its declaration does not have.",
    )


@pydantic.input(
    AssignInputModel,
    description="The input for assigning args to a action. A GraphQL assign is a ROOT by definition — children are created only over the agent socket (AssignRequest, where parent is mandatory) and by server-internal paths like init hooks, so parent/dependency/method are deliberately absent here.",
)
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
    policy: enums.TaskPolicy | None = None
    state_requires: list[StateRequirementInput] | None = None


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

    drawers: list[str] = Field(description="The drawers to collect: each an ID or a resource ID (as agent-minted drawers are referenced), within the caller's organization")


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


class DecideInputModel(BaseModel):
    """Base model for answering a held task.

    Attributes:
        task: ID of the held task
        approved: whether it may go on
        answer: what the person says along with it
    """

    task: str = Field(description="The held task: a child an agent asked for, or a task that holds itself")
    approved: bool = Field(description="Approve (the child runs; a task's own hold carries on approved) or deny (the child ends DENIED without running; a task's own hold carries on denied)")
    answer: str | None = Field(default=None, description="What the person says with the decision. A child gets it as its Assign's message; a task's own hold gets it back.")
    values: dict[str, JSON] | None = Field(default=None, description="The answer through the blok a task asks with (a hold on a blok), by the blok's returns, and checked against them. Only an approved ask takes them.")


@pydantic.input(DecideInputModel, description="The input for deciding a held task.")
class DecideInput:
    task: strawberry.ID
    approved: bool
    answer: str | None = None
    values: scalars.Args | None = None


class InterruptInputModel(BaseModel):
    """Base model for interrupting a task.

    Attributes:
        task: ID of the task to interrupt
    """

    task: str = Field(description="The task ID to interrupt")


@pydantic.input(InterruptInputModel, description="The input for interrupting a task.")
class InterruptInput:
    task: strawberry.ID
