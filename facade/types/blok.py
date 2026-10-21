"""Bloks, blok dependencies, materialized bloks and agent mappings."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import TYPE_CHECKING, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models, scalars
from facade.blok_revisions import content_of_blok, content_of_revision
from facade.types.base import build_prescoped_queryset, row_of
from facade.types.demand import ActionDependencyModel, StateDependencyModel
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.inputs import models as rimodels
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.auth import App, User
    from facade.types.dashboard import DashboardPlacement, UICatalog
    from facade.types.task import Task
    from facade.types.demand import ActionDependency, StateDependency
    from facade.types.threed import Placement


def blok_hash(components: object, demo_state: object, context: str) -> str:
    """The hash of a blok's rendering: canonical JSON (sorted keys, no whitespace) of its parts."""
    canonical = json.dumps({"components": components, "demo_state": demo_state, "context": context}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@strawberry_django.type(models.Blok, filters=filters.BlokFilter, pagination=True)
class Blok:
    id: strawberry.ID
    name: str
    description: str | None
    creator: User
    catalog: UICatalog
    app: App | None = strawberry_django.field(description="The app whose agents provide this blok; null for a custom blok a user made. A provided blok changes and goes with its agents' registrations and cannot be edited or materialized by hand: fork it.")

    @strawberry_django.field(description="Whether an app's agents provide this blok (see `app`).", only=["app_id"])
    def provided(self) -> bool:
        return row_of(self, models.Blok).app_id is not None

    @strawberry_django.field(description="The agents that currently provide this blok, each through its own materialization.")
    def providers(self, info: Info) -> list[Agent]:
        return cast("list[Agent]", list(models.Agent.objects.filter(declared_materialized_bloks__blok=row_of(self, models.Blok)).distinct()))

    materialized_bloks: list["MaterializedBlok"] = strawberry_django.field(
        description="Materialized bloks that are instances of this blok.",
    )
    dependencies: list["BlokDependency"] = strawberry_django.field(
        description="Dependencies that need to be resolved for this blok.",
    )

    @strawberry_django.field(description="The renderer for this blok's root.")
    def context(self) -> renums.ComponentContext:
        return renums.ComponentContext(self.context)

    @strawberry_django.field(description="The items this blok is opened for, declared like an action's args and read under `@args.<key>`; empty for a blok that is opened on its own.")
    def args(self) -> list[rtypes.ArgPort]:
        return cast("list[rtypes.ArgPort]", [rmodels.ArgPortModel(**i) for i in self.args])

    for_model: str | None = strawberry_django.field(description="The model reference this blok decorates, if any.")
    for_node: str | None = strawberry_django.field(description="The node of `forModel` this blok attaches to; the model's root when null.")
    origin: str | None = strawberry_django.field(description="Where this custom blok was installed from (e.g. kabinet's `RepoBlok.origin`); null for one made here.")
    origin_hash: str | None = strawberry_django.field(description="The hash its origin gave what it draws when it was installed. When the origin's hash differs now, it has an update.")

    @strawberry_django.field(description="The whole component tree as one JSON value, in the wire spelling (snake_case keys). Unlike `components` it has no depth limit, which a selection set always has.")
    def component_tree(self) -> rscalars.AnyDefault:
        return cast("rscalars.AnyDefault", self.components)

    @strawberry_django.field(description="The typed component tree of this blok.")
    def components(self) -> list[rtypes.ComponentNode]:
        return cast("list[rtypes.ComponentNode]", [rmodels.ComponentNodeModel(**i) for i in self.components])

    @strawberry_django.field(description="Demo state used to render a preview of this blok without agents.")
    def demo_state(self) -> scalars.Props:
        return self.demo_state

    @strawberry_django.field(
        description="A SHA-256 of what the blok renders: its component tree, demo state and context. Two bloks with the same hash draw the same thing, so a client may cache what it rendered under it.",
        only=["components", "demo_state", "context"],
    )
    def hash(self) -> str:
        row = row_of(self, models.Blok)
        return blok_hash(row.components, row.demo_state, row.context)

    @strawberry_django.field(description="Non-fatal registration findings, e.g. manifest util calls naming operations that neither the base catalog nor this blok's catalog provides.")
    def diagnostics(self) -> list[rtypes.Diagnostic]:
        return cast("list[rtypes.Diagnostic]", [rmodels.DiagnosticModel(**i) for i in self.diagnostics])

    head: BlokRevision | None = strawberry_django.field(description="The revision this custom blok is at: what an edit names as its `base`. Null for a provided blok, which changes with its agents' registrations instead.")

    @strawberry_django.field(description="The revisions of this custom blok, newest first. An undo is a revision too, so history only grows.")
    def revisions(self, limit: int = 20, offset: int = 0) -> list[BlokRevision]:
        rows = row_of(self, models.Blok).revisions.order_by("-number")[offset : offset + limit]
        return cast("list[BlokRevision]", list(rows))

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Blok], info: Info, **kwargs: object) -> QuerySet[models.Blok]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry.type(description="A dependency as a blok revision declares it.")
class RevisionDependency:
    key: str
    optional: bool
    description: str | None
    app: str | None = strawberry.field(description="Only agents of this app may be bound to it.")
    version: str | None = strawberry.field(description="Only agents of this version may be bound to it.")


@strawberry_django.type(models.BlokRevision, description="One state of a custom blok, never changed once written. Every write to a custom blok commits one; the blok is a copy of its head.")
class BlokRevision:
    id: strawberry.ID
    number: int = strawberry_django.field(description="The place of this revision in its blok's history, from 1.")
    blok: Blok
    parent: BlokRevision | None = strawberry_django.field(description="The revision this one was made from; null for the first.")
    catalog: UICatalog
    content_hash: str = strawberry_django.field(description="sha256 of everything this revision holds, its dependencies included (unlike `hash`).")
    author: User | None = strawberry_django.field(description="The user it was written as.")
    task: Task | None = strawberry_django.field(description="The task that wrote it, e.g. the assistant turn that designed it.")
    message: str | None = strawberry_django.field(description="What changed and why, in a sentence.")
    created_at: datetime.datetime

    @strawberry_django.field(description="The renderer for this revision's root.")
    def context(self) -> renums.ComponentContext:
        return renums.ComponentContext(self.context)

    @strawberry_django.field(description="The whole component tree as one JSON value, in the wire spelling (snake_case keys).")
    def component_tree(self) -> rscalars.AnyDefault:
        return cast("rscalars.AnyDefault", self.components)

    @strawberry_django.field(description="The typed component tree of this revision.")
    def components(self) -> list[rtypes.ComponentNode]:
        return cast("list[rtypes.ComponentNode]", [rmodels.ComponentNodeModel(**i) for i in self.components])

    @strawberry_django.field(description="The local state of this revision.")
    def demo_state(self) -> scalars.Props:
        return self.demo_state

    @strawberry_django.field(description="A SHA-256 of what this revision renders, as `Blok.hash` is of the head.", only=["components", "demo_state", "context"])
    def hash(self) -> str:
        row = row_of(self, models.BlokRevision)
        return blok_hash(row.components, row.demo_state, row.context)

    @strawberry_django.field(description="The agent dependencies of this revision.")
    def dependencies(self) -> list[RevisionDependency]:
        return [_revision_dependency(dependency) for dependency in content_of_revision(row_of(self, models.BlokRevision)).dependencies]

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.BlokRevision], info: Info, **kwargs: object) -> QuerySet[models.BlokRevision]:
        return build_prescoped_queryset(info, queryset, field="blok__organization")


def _revision_dependency(dependency: rimodels.AgentDependencyInputModel) -> RevisionDependency:
    return RevisionDependency(key=dependency.key, optional=dependency.optional, description=dependency.description, app=dependency.app, version=dependency.version)


@strawberry_django.type(models.BlokDependency, filters=filters.BlokDependencyFilter, pagination=True, description="An agent dependency declared by a blok.")
class BlokDependency:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the dependency.")
    blok: Blok = strawberry_django.field(description="The blok that declares this dependency.")
    key: str = strawberry_django.field(description="Optional string identifier or tag for reference.")
    optional: bool = strawberry_django.field(description="Indicates if the dependency is optional.")
    description: str | None = strawberry_django.field(description="Optional description of the dependency.")
    auto_resolvable: bool = strawberry.field(
        default=False,
        description="Whether this dependency is auto resolvable or not. If so we will try to automatically resolve it based on the demands specified in the dependency and the capabilities of the available agents in the system. This is used to identify the demand in the system. Attention if any of the dependencies of this agent dependency is not auto resolvable, this dependency will also not be auto resolvable",
    )
    app_filter: str | None = strawberry_django.field(
        default=None,
        description="Optional filter string to limit which agents can be bound to this dependency based on the app they belong to. The filter string should be in the format 'app_identifier:version' where version can be a specific version or a wildcard '*'. For example, 'my_app:*' would allow any agent belonging to 'my_app' regardless of version, while 'my_app:1.0.0' would only allow agents with that specific version.",
    )
    version_filter: str | None = strawberry_django.field(
        default=None,
        description="Optional filter string to limit which agents can be bound to this dependency based on the version of the app they belong to. The filter string should be in the format 'version' where version can be a specific version or a wildcard '*'. For example, '*' would allow any version, while '1.0.0' would only allow agents with that specific version.",
    )
    min_viable_instances: int | None = strawberry_django.field(
        default=None,
        description="Minimum number of viable agent instances required to resolve this dependency. This is used in combination with the auto_resolvable field to determine if a dependency can be automatically resolved. If the number of available agent instances that match the filters is less than this number, the dependency will not be considered auto resolvable.",
    )
    max_viable_instances: int | None = strawberry_django.field(
        default=None,
        description="Maximum number of viable agent instances that can be bound to this dependency. This is used in combination with the auto_resolvable field to determine if a dependency can be automatically resolved. If the number of available agent instances that match the filters is greater than this number, the dependency will not be considered auto resolvable.",
    )

    @strawberry_django.field(description="List of action demands specified in this dependency.")
    def singular(self) -> bool:
        """Whether this dependency is singular or not. A singular dependency is a dependency that can only be resolved to one agent, meaning that if there are multiple implementations that match the filters and demands of this dependency, it will not be considered singular."""
        return self.min_viable_instances == 1 and (self.max_viable_instances is None or self.max_viable_instances == 1)

    @strawberry_django.field(description="The named action requirements of this dependency.")
    def action_dependencies(self) -> list["ActionDependency"]:
        # get_action_dependencies normalizes legacy flat JSON into the demand wrapper.
        row = row_of(self, models.BlokDependency)
        return cast("list[ActionDependency]", [ActionDependencyModel(**d.model_dump()) for d in row.get_action_dependencies()])

    @strawberry_django.field(description="The named state requirements of this dependency.")
    def state_dependencies(self) -> list["StateDependency"]:
        row = row_of(self, models.BlokDependency)
        return cast("list[StateDependency]", [StateDependencyModel(**d.model_dump()) for d in row.get_state_dependencies()])


@strawberry_django.type(models.MaterializedBlok, filters=filters.MaterializedBlokFilter, pagination=True, ordering=filters.MaterializedBlokOrder, description="A materialized instance of a Blok that can be placed on dashboards and linked to agent states.")
class MaterializedBlok:
    id: strawberry.ID
    blok: Blok
    name: str | None
    description: str | None
    is_default: bool = strawberry_django.field(description="Whether this is its declaring agent's default blok for the blok's context.")
    declared_by: Agent | None = strawberry_django.field(description="The agent this materialization belongs to, for a blok its app provides; null when a user made it. An agent's materialization follows its registrations.")
    created_at: datetime.datetime
    updated_at: datetime.datetime
    agent_mappings: list["BlokAgentMapping"] = strawberry_django.field(
        description="Mappings of states to this materialized blok.",
    )
    dashboard_placements: list["DashboardPlacement"] = strawberry_django.field(
        description="Placements of this materialized blok on dashboards.",
    )
    placements: list["Placement"] = strawberry_django.field(
        description="Placements of this materialized blok.",
    )
    pinned_revision: BlokRevision | None = strawberry_django.field(description="The revision this materialization stays at; null when it follows its blok's head.")

    @strawberry_django.field(description="The revision this materialization shows: the pinned one, else its blok's head. Null for a provided blok, which has none.")
    def revision(self) -> BlokRevision | None:
        row = row_of(self, models.MaterializedBlok)
        return cast("BlokRevision | None", row.pinned_revision or row.blok.head)

    @strawberry_django.field(description="The required dependencies of the revision shown that no agent is bound to here. An edit made elsewhere can add one: bind it with updateMaterializedBlok.")
    def unbound_dependencies(self) -> list[str]:
        row = row_of(self, models.MaterializedBlok)
        revision = row.pinned_revision or row.blok.head
        content = content_of_blok(row.blok) if revision is None else content_of_revision(revision)
        bound = set(row.agent_mappings.values_list("key", flat=True))
        return sorted(dependency.key for dependency in content.dependencies if not dependency.optional and dependency.key not in bound)

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.MaterializedBlok], info: Info, **kwargs: object) -> QuerySet[models.MaterializedBlok]:
        return build_prescoped_queryset(info, queryset, field="blok__organization")


@strawberry_django.type(models.BlokAgentMapping)
class BlokAgentMapping:
    id: strawberry.ID
    key: str
    agent: Agent

    materialized_blok: MaterializedBlok
    created_at: datetime.datetime
    updated_at: datetime.datetime


@strawberry.type(description="What an edit of a blok did, or with `dryRun` would do.")
class BlokEdit:
    applied: bool = strawberry.field(description="Whether anything was written; false for a dry run.")
    forked: bool = strawberry.field(description="Whether the blok was provided and the edit made a custom copy of it to land on: `blok` is the copy, and an edit made through a materialization has a new one of it.")
    blok: Blok = strawberry.field(description="The blok the edit landed on: the one edited, or the custom copy of a provided one. An editor of the blok follows it here.")
    revision: BlokRevision | None = strawberry.field(description="The revision the blok is at after the edit; null for a dry run.")
    materialized_blok: MaterializedBlok | None = strawberry.field(description="The materialization that shows the edit: the one it was made through, or the fork's new one. Null for an edit of the blok itself.")
    diagnostics: list[rtypes.Diagnostic] = strawberry.field(description="Non-fatal findings about the new content, e.g. util calls no catalog provides.")
