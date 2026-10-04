"""takt's internal API, as types: what each route is sent and what it answers.

The contract is the table at the top of ``takt/crates/rekuest-server/src/internal.rs``. A
:class:`Route` pairs a path with its request and answer, so :func:`facade.takt.call` knows
what it may be given and what comes back. Ids go out as takt accepts them (numbers or numeric
strings) and come back as strings.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from facade import models
from facade.caller_context import CallerContext
from facade.inputs import AssignInputModel, CreateHigherOrderImplementationInputModel, DependencyTreeInputModel, ImplementAgentInputModel, ProbeInputModel


class Request(BaseModel):
    """What a route is sent. A field left ``None`` is left out, unless the route says otherwise."""

    #: Whether only the fields that were given go out, nulls included. For a route where a
    #: present null means something (``agent/ensure``: it clears).
    only_what_was_set: ClassVar[bool] = False

    def body(self) -> bytes:
        """The request as takt reads it."""
        if self.only_what_was_set:
            return self.model_dump_json(exclude_unset=True).encode()
        return self.model_dump_json(exclude_none=True).encode()


class Answer(BaseModel):
    """What a route answers. takt may say more than this server reads."""

    model_config = ConfigDict(extra="ignore")


@dataclass(frozen=True)
class Route[Req: Request, Ans: Answer]:
    """One route of the internal API: its path under ``internal/``, and what it answers."""

    path: str
    answer: type[Ans]
    request: type[Req]


class Principal(BaseModel):
    """The requesting identity, as primary keys and roles."""

    user: int
    client: int | None = None
    organization: int | None = None
    roles: list[str] = []

    @classmethod
    def of(cls, context: CallerContext) -> Principal:
        """A request's (or a service agent's) identity."""
        return cls(
            user=context.user.pk,
            client=context.client.pk if context.client is not None else None,
            organization=context.organization.pk if context.organization is not None else None,
            roles=list(context.roles),
        )

    @classmethod
    def of_caller(cls, caller: models.Caller) -> Principal:
        """A recorded caller, which carries no roles."""
        return cls(user=caller.user_id, client=caller.client_id, organization=caller.organization_id)


# --- tasks -----------------------------------------------------------------------------------


class AssignRequest(Request):
    principal: Principal
    input: AssignInputModel


class AssignAnswer(Answer):
    task: str
    reference: str | None = None
    created: bool


class ControlRequest(Request):
    """A control on a task. Without a principal it is the server's own, and trusted."""

    task: str
    principal: Principal | None = None
    step: bool | None = None


class TaskAnswer(Answer):
    task: str


class CollectRequest(Request):
    principal: Principal
    drawers: list[str]


class CollectAnswer(Answer):
    drawers: list[str]


# --- the dependency tree ---------------------------------------------------------------------


class DependencyNote(Answer):
    """What a dry run says about one dependency: the row that declares it, and why it is unmet."""

    dependency: str | None = None
    unmet: str | None = None


class DependencyLevel(Answer):
    """One level of a dependency tree: per dependency key, the agents it binds."""

    dependencies: dict[str, list[BoundAgent] | None] = {}
    meta: dict[str, DependencyNote] = {}


class BoundImplementation(DependencyLevel):
    """The implementation an action is bound to, with the level below it."""

    implementation: str | None = None


class BoundAgent(Answer):
    agent: str
    actions: dict[str, BoundImplementation] = {}


class ResolveRequest(Request):
    principal: Principal
    input: DependencyTreeInputModel


class ResolveAnswer(DependencyLevel):
    satisfied: bool = False


# --- agents ----------------------------------------------------------------------------------


class AgentRequest(Request):
    principal: Principal
    agent: str
    reason: str | None = None


class AgentAnswer(Answer):
    agent: str


class EnsureAgentRequest(Request):
    """Only what is given is changed; a field given as ``None`` is cleared."""

    only_what_was_set: ClassVar[bool] = True

    principal: Principal
    name: str | None = None
    description: str | None = None
    kind: str | None = None
    hook_url: str | None = None
    hook_url_secret: str | None = None
    clear_drawers: bool | None = None


class ImplementAgentRequest(Request):
    principal: Principal
    input: ImplementAgentInputModel


class ImplementAnswer(Answer):
    agent: str


class DeleteImplementationRequest(Request):
    principal: Principal
    implementation: str


class ImplementationAnswer(Answer):
    implementation: str


class CreateHigherOrderRequest(Request):
    principal: Principal
    input: CreateHigherOrderImplementationInputModel


class CleanupActionsRequest(Request):
    principal: Principal
    actions: list[str] | None = None


class CleanupActionsAnswer(Answer):
    deleted: int


# --- probes ----------------------------------------------------------------------------------


class ProbeRequest(Request):
    principal: Principal
    input: ProbeInputModel


class ProbeControlRequest(Request):
    principal: Principal
    probe: str


class ProbeState(Answer):
    """A probe's state: the redis hash takt keeps (``facade/probes/store.py``), with its id.

    Everything in the hash is a string; ``done`` is the terminal kind once there is one.
    """

    id: str
    agent: str = ""
    caller: str = ""
    org: str = ""
    action: str = ""
    impl: str = ""
    iface: str = ""
    ref: str = ""
    kind: str = "QUEUED"
    seq: int = 0
    done: str = ""
    last_returns: str = ""
    err: str = ""
    created: str = ""


# --- schedules and triggers ------------------------------------------------------------------


class Timing(Request):
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str


class UpcomingTiming(Timing):
    created_at: datetime.datetime
    count: int


class UpcomingRequest(Request):
    timings: list[UpcomingTiming]


class UpcomingSlots(Answer):
    """One timing's next slots, or why takt cannot read the timing."""

    slots: list[datetime.datetime] = []
    error: str | None = None


class UpcomingAnswer(Answer):
    upcoming: list[UpcomingSlots]


class ScheduleRequest(Request):
    schedule: str


class Nothing(Answer):
    """A route that only succeeds or refuses."""


class FireTriggerRequest(Request):
    principal: Principal
    trigger: str
    signal: str


class FiringAnswer(Answer):
    firing: str


# --- drawers ---------------------------------------------------------------------------------


class ShelveRequest(Request):
    principal: Principal
    identifier: str
    resource_id: str
    label: str | None = None
    description: str | None = None


class UnshelveRequest(Request):
    principal: Principal
    id: str


class DrawerAnswer(Answer):
    drawer: str


ASSIGN = Route("assign", AssignAnswer, AssignRequest)
RESOLVE = Route("resolve", ResolveAnswer, ResolveRequest)
CANCEL = Route("cancel", TaskAnswer, ControlRequest)
INTERRUPT = Route("interrupt", TaskAnswer, ControlRequest)
PAUSE = Route("pause", TaskAnswer, ControlRequest)
RESUME = Route("resume", TaskAnswer, ControlRequest)
BOUNCE = Route("bounce", AgentAnswer, AgentRequest)
KICK = Route("kick", AgentAnswer, AgentRequest)
BLOCK = Route("block", AgentAnswer, AgentRequest)
UNBLOCK = Route("unblock", AgentAnswer, AgentRequest)
COLLECT = Route("collect", CollectAnswer, CollectRequest)
PROBE = Route("probe", ProbeState, ProbeRequest)
PROBE_CANCEL = Route("probe/cancel", ProbeState, ProbeControlRequest)
PROBE_PAUSE = Route("probe/pause", ProbeState, ProbeControlRequest)
PROBE_RESUME = Route("probe/resume", ProbeState, ProbeControlRequest)
ENSURE_AGENT = Route("agent/ensure", AgentAnswer, EnsureAgentRequest)
IMPLEMENT_AGENT = Route("agent/implement", ImplementAnswer, ImplementAgentRequest)
DELETE_AGENT = Route("agent/delete", AgentAnswer, AgentRequest)
DELETE_IMPLEMENTATION = Route("implementation/delete", ImplementationAnswer, DeleteImplementationRequest)
CREATE_HIGHER_ORDER = Route("higher-order/create", ImplementationAnswer, CreateHigherOrderRequest)
CLEANUP_ACTIONS = Route("action/cleanup", CleanupActionsAnswer, CleanupActionsRequest)
VALIDATE_TIMING = Route("schedule/validate", Nothing, Timing)
TRIGGER_SCHEDULE = Route("schedule/trigger", TaskAnswer, ScheduleRequest)
UPCOMING = Route("schedule/upcoming", UpcomingAnswer, UpcomingRequest)
FIRE_TRIGGER = Route("trigger/fire", FiringAnswer, FireTriggerRequest)
SHELVE = Route("drawer/shelve", DrawerAnswer, ShelveRequest)
UNSHELVE = Route("drawer/unshelve", DrawerAnswer, UnshelveRequest)
