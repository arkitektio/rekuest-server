"""Task types, their statistics resolver, events and instructs."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, List, Optional, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import enums, filters, models, scalars
from facade.takt_api import DependencyLevel
from facade.types.base import build_prescoped_queryset, row_of
from facade.types.dependency import ResolvedAgentDependency, resolved_level
from rekuest_core import scalars as rscalars

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.action import Action
    from facade.types.agent import Agent, Lock
    from facade.types.auth import Caller
    from facade.types.blok import ActionBlok, MaterializedBlok
    from facade.types.dependency import Resolution
    from facade.types.implementation import Implementation
    from facade.types.schedule import Schedule
    from facade.types.threed import Space
    from facade.types.trigger import Signal, Trigger


@strawberry.type(description="A blok a task showed a person: to ask (the task holds until they answer through it) or to inform.")
class TaskBlok:
    materialized_blok: "MaterializedBlok" = strawberry.field(description="What it shows: a materialization of scope TASK, opened on its args.")
    role: enums.TaskBlokRole = strawberry.field(description="ASK or INFORM.")
    status: enums.TaskBlokStatus = strawberry.field(description="Where it stands: an ask WAITING, ANSWERED, DISMISSED or GONE (the task ended first); an inform SHOWN.")
    message: str | None = strawberry.field(description="What the task said with it.")
    values: scalars.Args | None = strawberry.field(description="The answer to an ask, by the blok's returns.")
    decided_by: str | None = strawberry.field(description="The user (sub) who answered or dismissed an ask.")
    created_at: datetime.datetime = strawberry.field(description="When the task showed it.")


@strawberry.type(description="An agent a task got for one of its action's dependencies, as it was resolved when the task was assigned.")
class TaskDependencyAgent:
    key: str = strawberry.field(description="The dependency key, as the action's implementation declares it and the action's bloks name it.")
    agent: "Agent" = strawberry.field(description="The agent bound to it for this run. A key bound to several agents comes once per agent.")


def dependency_agents(task: models.Task) -> list[TaskDependencyAgent]:
    """The top level of the dependency tree frozen on ``task`` (``Task.dependencies``): per key, the agents it got, in order."""
    level = DependencyLevel.model_validate({"dependencies": task.dependencies or {}})
    bound = [(key, agent.agent) for key, agents in level.dependencies.items() for agent in agents or []]
    agents = models.Agent.objects.filter(id__in=[agent for _, agent in bound if agent.isdigit()], organization_id__in=models.Agent.objects.filter(pk=task.agent_id).values("organization_id")).in_bulk()
    return [TaskDependencyAgent(key=key, agent=cast("Agent", agents[int(agent)])) for key, agent in bound if agent.isdigit() and int(agent) in agents]


def task_bloks(task: models.Task) -> list[TaskBlok]:
    """The bloks ``task`` showed, oldest first, each with where it stands (read off its events).

    An ask's ``HELD`` event names its materialization; the ``DECIDED`` that follows it is its
    answer. An inform's ``INFORM`` event names it.
    """
    shown = list(task.shown_bloks.all().order_by("id"))
    if not shown:
        return []
    events = list(task.events.filter(kind__in=["HELD", "DECIDED", "INFORM"]).order_by("id").values("kind", "value", "message", "created_at"))
    bloks = []
    for materialized in shown:
        key = str(materialized.id)
        named = next((i for i, e in enumerate(events) if e["kind"] in ("HELD", "INFORM") and (e["value"] or {}).get("blok") == key), None)
        event = events[named] if named is not None else None
        role = enums.TaskBlokRole(materialized.task_role or "INFORM")
        values, decided_by = None, None
        if role == enums.TaskBlokRole.INFORM:
            status = enums.TaskBlokStatus.SHOWN
        else:
            decision = next((e for e in events[(named or 0) + 1 :] if e["kind"] == "DECIDED"), None) if named is not None else None
            if decision is not None:
                answer = decision["value"] or {}
                status = enums.TaskBlokStatus.ANSWERED if answer.get("approved") else enums.TaskBlokStatus.DISMISSED
                values, decided_by = answer.get("values"), answer.get("user")
            elif task.is_done:
                status = enums.TaskBlokStatus.GONE
            else:
                status = enums.TaskBlokStatus.WAITING
        bloks.append(
            TaskBlok(
                materialized_blok=cast("MaterializedBlok", materialized),
                role=role,
                status=status,
                message=event["message"] if event else None,
                values=cast("scalars.Args | None", values),
                decided_by=decided_by,
                created_at=event["created_at"] if event else materialized.created_at,
            )
        )
    return bloks


@strawberry_django.type(models.Task, filters=filters.TaskFilter, ordering=filters.TaskOrder, pagination=True, description="Tracks the assignment of an implementation to a specific task.")
class Task:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the task.")
    reference: str | None = strawberry_django.field(description="Optional external reference for tracking.")
    is_done: bool = strawberry_django.field(description="Indicates if the task is completed.")
    args: rscalars.AnyDefault = strawberry_django.field(description="Arguments used in the task.")
    held_locks: list["Lock"] = strawberry_django.field(description="The locks this task holds right now.")
    args_hash: str | None = strawberry_django.field(description="Canonical sha256 of the assign args — the replay-discovery key.")
    dependencies: rscalars.AnyDefault = strawberry_django.field(description="The used dependencies for this assignemnet")
    resolution: Optional["Resolution"] = strawberry.field(description="Resolution used to resolve dependencies for this task.")
    root: Optional["Task"] = strawberry.field(description="Root task in the creation chain.")
    parent: Optional["Task"] = strawberry.field(description="Parent task that triggered this one.")
    parent_step: int | None = strawberry_django.field(description="The parent's step this child took; null for roots and for children of agents without numbering.")
    call_key: str | None = strawberry_django.field(description="What the parent calls this child; null for roots and for keyless agents.")
    action: "Action" = strawberry.field(description="Action assigned.")
    capture: bool = strawberry.field(description="Indicates if the task is being captured for logging or debugging.")
    implementation: Optional["Implementation"] = strawberry.field(description="Implementation assigned to execute. Null until the task is mapped to one.")
    latest_event_kind: enums.TaskEventKind = strawberry.field(description="Type of the latest event.")
    latest_instruct_kind: enums.TaskInstructKind = strawberry.field(description="Last instruction type.")
    policy: enums.TaskPolicy = strawberry.field(description="Whether the work agents ask for under this task waits for a person (ASK) or runs without asking (AUTO, auto-mode). A root's is set on its assign; a child takes its parent's.")
    caller: Optional["Caller"] = strawberry.field(description="Caller that created this task.")
    created_at: datetime.datetime = strawberry_django.field(description="Creation timestamp.")
    updated_at: datetime.datetime = strawberry_django.field(description="Last update timestamp.")
    revision: int = strawberry_django.field(description="Monotonic per-task version, bumped by every write. Pairs with the change feeds: discard a TaskChange whose revision is not greater than the one you hold.")
    finished_at: datetime.datetime | None = strawberry.field(description="Timestamp when the task was finished.")
    latest_progress: int | None = strawberry_django.field(description="Progress (0-100) of the latest PROGRESS event that carried one. Kept as events are recorded: follow a task with this, not with its events.")
    latest_message: str | None = strawberry_django.field(description="Message of the latest event that carried one.")
    latest_returns: rscalars.AnyDefault | None = strawberry_django.field(description="Returns of the latest YIELD event.")

    @strawberry_django.field(description="The bloks this task showed a person, oldest first: asks it holds on (or held on) and informs.")
    def bloks(self) -> list[TaskBlok]:
        return task_bloks(row_of(self, models.Task))

    @strawberry_django.field(description="Its action's own bloks (`Action.bloks`), at most one per context: how a client shows this run, with each dependency key bound by `dependencyAgents`.")
    def action_bloks(self) -> list["ActionBlok"]:
        return cast("list[ActionBlok]", list(models.ActionBlok.objects.filter(action_id=row_of(self, models.Task).action_id).order_by("context")))

    @strawberry_django.field(description="The agents this run got for its action's dependencies, by key: the top level of the tree resolved when it was assigned. What an action blok's `<At dependency>` anchors at.")
    def dependency_agents(self) -> list[TaskDependencyAgent]:
        return dependency_agents(row_of(self, models.Task))

    space: Optional["Space"] = strawberry_django.field(description="The space the caller started this run in (the assign's `space`): where a client draws the action's overlay. Null when none was named, or the space is gone.")
    schedule: Optional["Schedule"] = strawberry.field(description="The schedule this task is a run of, if any.")
    signal: Optional["Signal"] = strawberry.field(description="The signal that caused this task, if a trigger fired it.")
    trigger: Optional["Trigger"] = strawberry.field(description="The trigger that fired this task, if any.")
    not_before: datetime.datetime | None = strawberry.field(description="The task is held back until then (a delayed task); null = dispatched on creation.")
    acted_on: List[str] = strawberry.field(description="List of resources or entities this task acted upon.")
    children: List["Task"] = strawberry.field(description="Child tasks spawned from this one.")
    agent: Agent | None = strawberry.field(description="Agent responsible for this task.")
    events: list["TaskEvent"] = strawberry_django.field(description="The events")
    dependency: str | None = strawberry_django.field(description="The dependency thats linked to the parents execution if applicable.")
    dependency_method: str | None = strawberry_django.field(description="The method of the dependency that caused this task, if applicable.")

    @strawberry_django.field(description="List of recent instructions for this task.")
    def instructs(self) -> list["TaskInstruct"]:
        return self.instructs.order_by("-created_at")[:10]

    @strawberry_django.field(description="Get a specific argument by key.")
    def arg(self, key: str) -> scalars.Args | None:
        row = row_of(self, models.Task)
        return row.args.get(key, None)

    @strawberry_django.field(description="The resolved dependencies for this task.")
    def resolved_dependencies(self) -> List[ResolvedAgentDependency]:
        # The tree takt resolved when the task was assigned, as it stored it.
        row = row_of(self, models.Task)
        return resolved_level(DependencyLevel.model_validate({"dependencies": row.dependencies or {}}), row.implementation_id)

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Task], info: Info, **kwargs: object) -> QuerySet[models.Task]:
        # Scope through the non-null ``agent`` FK, not ``implementation__action`` — the latter is
        # nullable, so its INNER JOIN silently dropped every not-yet-mapped (QUEUED/BOUND) task
        # and disagreed with the ``agent__organization`` scope ``TaskStats`` uses below.
        return build_prescoped_queryset(info, queryset, field="agent__organization")


@strawberry_django.type(models.TaskEvent, filters=filters.TaskEventFilter, ordering=filters.TaskEventOrder, pagination=True, description="An event that occurred during a task.")
class TaskEvent:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the event.")
    returns: rscalars.AnyDefault | None = strawberry_django.field(description="Optional return values.")
    task: "Task" = strawberry_django.field(description="Associated task.")
    kind: enums.TaskEventKind = strawberry_django.field(description="Kind of task event.")
    message: str | None = strawberry_django.field(description="Optional message associated with the event.")
    progress: int | None = strawberry_django.field(description="Progress percentage.")
    created_at: strawberry.auto = strawberry_django.field(description="Time when event was created.")
    delegated_to: Optional["Task"] = strawberry_django.field(description="If this event was delegated, the task it was delegated to.")
    agent_pos: int | None = strawberry_django.field(description="The session position (pos) of the report that wrote this event; null for server-written events and agents without numbering.")
    agent_ts: datetime.datetime | None = strawberry_django.field(description="When the agent recorded the report; null for server-written events and agents without numbering.")
    step: int | None = strawberry_django.field(description="The report's step within its task; a task's history in step order. Null for server-written events.")
    effect: str | None = strawberry_django.field(description="EFFECT events: NOW, RANDOM, SLEEP or RECORD.")
    key: str | None = strawberry_django.field(description="EFFECT events: what the task calls this value; a replay matches values by key.")
    value: rscalars.AnyDefault | None = strawberry_django.field(description="EFFECT events: the value the task took (NOW: epoch seconds, RANDOM: hex, SLEEP: deadline). HELD: what waits for a decision ({kind: \"assign\", prompt, action, agent, args, caller_task, caller_agent} for a child an agent asked for, with held: the child's id on its parent and root; {kind: \"self\", prompt, details} for a task's own hold). DECIDED: the decision ({approved, answer, auto, user}). DENIED: {answer, user}. FAILED: {code} when the failure has one the protocol names (STATE_PRECONDITION: the agent's state did not match the task's stateRequires, and nothing of it ran). LOST: what is known about the lost run.")

    @strawberry_django.field(description="Log level of the event (LOG events; INFO when unset).")
    def level(self) -> enums.LogLevel:
        return enums.LogLevel(str(self.level)) if self.level else enums.LogLevel.INFO

    @strawberry_django.field(description="Reference string for the event.")
    def reference(self) -> str | None:
        return self.task.reference


@strawberry_django.type(models.TaskInstruct, filters=filters.TaskInstructFilter, pagination=True, description="An instruct event for a specific task.")
class TaskInstruct:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the instruct event.")
    task: "Task" = strawberry_django.field(description="Task the instruction relates to.")
    kind: enums.TaskInstructKind = strawberry_django.field(description="Type of instruction.")
    created_at: strawberry.auto = strawberry_django.field(description="Time when instruction was issued.")
