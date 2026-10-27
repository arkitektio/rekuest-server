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
from strawberry_django.fields.field import StrawberryDjangoField

from facade import enums, filters, models, scalars
from facade.blok_revisions import content_of_blok, content_of_revision, rendering_of
from facade.types.base import build_prescoped_queryset, row_of
from facade.types.demand import ActionDependencyModel, StateDependencyModel
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.inputs import models as rimodels
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.action import Action
    from facade.types.agent import Agent
    from facade.types.auth import App, User
    from facade.types.dashboard import DashboardPlacement, Module
    from facade.types.task import Task
    from facade.types.demand import ActionDependency, StateDependency
    from facade.types.threed import Placement, SpacePlacement


def blok_hash(row: models.Blok | models.BlokRevision) -> str:
    """The hash of a blok's rendering: canonical JSON (sorted keys, no whitespace) of what it draws."""
    parts = rendering_of(row.components, row.context, row.vocab, row.catalogs, row.locals, row.demo, row.queries, row.subscriptions)
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


RENDERING_FIELDS = ["components", "context", "vocab", "catalogs", "locals", "demo", "queries", "subscriptions"]

V2_FIELD_DOCS = {
    "vocab": "The blok language version it speaks; 2 is the only one.",
    "catalogs": "The catalogs it needs, as name@version ('ui@1', 'scene@1').",
    "locals": "Its locals as written: form state that feeds a call, written only through `bind`, read bare; each an arg port plus `persist`, its kind null when it was left out. A persisted one stays on the device, never on a server. What `hash` covers.",
    "resolved_locals": "Its locals as its tree resolves them, each with a kind (a left-out kind inferred from its default, the task argument it is passed to or the control bound to it; ANY when nothing tells). Derived from the rest on every write: what renderers type the locals with, never part of `hash`.",
    "demo": "What a preview renders it with, mirroring the roots it reads (deps, args, task, queries, subscriptions); null when it declares none.",
    "queries": "Its declared reads, read as `queries.<key>`.",
    "subscriptions": "Its declared streams, read as `subscriptions.<key>`.",
}


class ListedMaterializationsField(StrawberryDjangoField):
    """The materializations a list shows: not the ones made for a message or shown by a task, which only their message or task opens."""

    def get_queryset(self, queryset, info, **kwargs):  # noqa: ANN001, ANN201  strawberry_django's own signature
        hidden = [enums.MaterializationScopeChoices.MESSAGE, enums.MaterializationScopeChoices.TASK]
        return super().get_queryset(queryset.exclude(scope__in=hidden), info, **kwargs)


class ListedBloksField(StrawberryDjangoField):
    """The bloks a list shows: not the ones a task made from its BSX to show, which go with their task,
    nor an action's own, which its action shows (``Action.bloks``)."""

    def get_queryset(self, queryset, info, **kwargs):  # noqa: ANN001, ANN201  strawberry_django's own signature
        return super().get_queryset(queryset.filter(owner_task__isnull=True, held_by_action=False), info, **kwargs)


@strawberry_django.type(models.Blok, filters=filters.BlokFilter, pagination=True)
class Blok:
    id: strawberry.ID
    name: str
    description: str | None
    creator: User
    module: Module = strawberry_django.field(description="The module (an app's own UI extras) it renders against besides its catalogs.")
    app: App | None = strawberry_django.field(description="The app whose agents provide this blok; null for a custom blok a user made. A provided blok changes and goes with its agents' registrations and cannot be edited or materialized by hand: fork it.")

    @strawberry_django.field(description="Whether an app's agents provide this blok (see `app`).", only=["app_id"])
    def provided(self) -> bool:
        return row_of(self, models.Blok).app_id is not None

    held_by_action: bool = strawberry_django.field(description="Whether this blok is an action's own (`Action.bloks`): declared with the action, never listed, materialized or edited on its own.")

    @strawberry_django.field(description="The agents that currently provide this blok, each through its own materialization.")
    def providers(self, info: Info) -> list[Agent]:
        return cast("list[Agent]", list(models.Agent.objects.filter(declared_materialized_bloks__blok=row_of(self, models.Blok)).distinct()))

    materialized_bloks: list["MaterializedBlok"] = strawberry_django.field(
        field_cls=ListedMaterializationsField,
        description="Materialized bloks that are instances of this blok, but the ones made for a message.",
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

    @strawberry_django.field(description="What a person answers through this blok when a task asks with it, declared like an action's returns: the values `blok.answer` hands back. Empty for a blok that answers nothing.")
    def returns(self) -> list[rtypes.ReturnPort]:
        return cast("list[rtypes.ReturnPort]", [rmodels.ReturnPortModel(**i) for i in self.returns])

    vocab: int = strawberry_django.field(description=V2_FIELD_DOCS["vocab"])
    catalogs: list[str] = strawberry_django.field(description=V2_FIELD_DOCS["catalogs"])

    @strawberry_django.field(description=V2_FIELD_DOCS["locals"])
    def locals(self) -> list[rtypes.BlokLocal]:
        return cast("list[rtypes.BlokLocal]", [rmodels.BlokLocalModel(**i) for i in self.locals])

    @strawberry_django.field(description=V2_FIELD_DOCS["resolved_locals"])
    def resolved_locals(self) -> list[rtypes.ResolvedBlokLocal]:
        return cast("list[rtypes.ResolvedBlokLocal]", [rmodels.ResolvedBlokLocalModel(**i) for i in self.resolved_locals])

    @strawberry_django.field(description=V2_FIELD_DOCS["demo"])
    def demo(self) -> rtypes.BlokDemo | None:
        return cast("rtypes.BlokDemo", rmodels.BlokDemoModel(**self.demo)) if self.demo else None

    @strawberry_django.field(description=V2_FIELD_DOCS["queries"])
    def queries(self) -> list[rtypes.BlokQuery]:
        return cast("list[rtypes.BlokQuery]", [rmodels.BlokQueryModel(**i) for i in self.queries])

    @strawberry_django.field(description=V2_FIELD_DOCS["subscriptions"])
    def subscriptions(self) -> list[rtypes.BlokSubscription]:
        return cast("list[rtypes.BlokSubscription]", [rmodels.BlokSubscriptionModel(**i) for i in self.subscriptions])

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

    @strawberry_django.field(
        description="A SHA-256 of what the blok renders: its component tree, context, language version, catalogs, locals, demo, queries and subscriptions. Two bloks with the same hash draw the same thing, so a client may cache what it rendered under it.",
        only=RENDERING_FIELDS,
    )
    def hash(self) -> str:
        return blok_hash(row_of(self, models.Blok))

    @strawberry_django.field(description="The findings of its last write against its catalogs and module, with the blok vocabulary's codes (`catalog.props`, `palette.raw_colour`, ...): warnings only, since an error refuses the write.")
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


@strawberry_django.type(
    models.ActionBlok,
    description="An action's own blok for one context: how a client shows a run of the action there. It names devices only by the action's dependency keys: a run binds them through `Task.dependencyAgents`, a preview through the `dependencyTree` dry run.",
)
class ActionBlok:
    id: strawberry.ID
    action: Action = strawberry_django.field(description="The action whose runs it shows.")
    blok: Blok = strawberry_django.field(description="What it draws: a blok held by its actions only (`heldByAction`), shared by every action of the app declaring the same.")

    @strawberry_django.field(description="Where a client shows a run with it: HTML in a task panel, SCENE as a 3D view of its own, OVERLAY drawn into the space that hosts the run, over the agents placed there.")
    def context(self) -> renums.ComponentContext:
        return renums.ComponentContext(self.context)


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
    module: Module = strawberry_django.field(description="The module this revision renders against.")
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

    @strawberry_django.field(description="A SHA-256 of what this revision renders, as `Blok.hash` is of the head.", only=RENDERING_FIELDS)
    def hash(self) -> str:
        return blok_hash(row_of(self, models.BlokRevision))

    vocab: int = strawberry_django.field(description=V2_FIELD_DOCS["vocab"])
    catalogs: list[str] = strawberry_django.field(description=V2_FIELD_DOCS["catalogs"])

    @strawberry_django.field(description=V2_FIELD_DOCS["locals"])
    def locals(self) -> list[rtypes.BlokLocal]:
        return cast("list[rtypes.BlokLocal]", [rmodels.BlokLocalModel(**i) for i in self.locals])

    @strawberry_django.field(description=V2_FIELD_DOCS["resolved_locals"])
    def resolved_locals(self) -> list[rtypes.ResolvedBlokLocal]:
        return cast("list[rtypes.ResolvedBlokLocal]", [rmodels.ResolvedBlokLocalModel(**i) for i in self.resolved_locals])

    @strawberry_django.field(description=V2_FIELD_DOCS["demo"])
    def demo(self) -> rtypes.BlokDemo | None:
        return cast("rtypes.BlokDemo", rmodels.BlokDemoModel(**self.demo)) if self.demo else None

    @strawberry_django.field(description=V2_FIELD_DOCS["queries"])
    def queries(self) -> list[rtypes.BlokQuery]:
        return cast("list[rtypes.BlokQuery]", [rmodels.BlokQueryModel(**i) for i in self.queries])

    @strawberry_django.field(description=V2_FIELD_DOCS["subscriptions"])
    def subscriptions(self) -> list[rtypes.BlokSubscription]:
        return cast("list[rtypes.BlokSubscription]", [rmodels.BlokSubscriptionModel(**i) for i in self.subscriptions])


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
    space_placements: list["SpacePlacement"] = strawberry_django.field(description="Where this 3D blok is placed in spaces, each at its affine transform.")
    pinned_revision: BlokRevision | None = strawberry_django.field(description="The revision this materialization stays at; null when it follows its blok's head.")

    @strawberry_django.field(description="Where it lives: DASHBOARD, MESSAGE when it was made for one chat message to show inline, or TASK when a task showed it.")
    def scope(self) -> enums.MaterializationScope:
        return enums.MaterializationScope(row_of(self, models.MaterializedBlok).scope)

    @strawberry_django.field(description="Why its task showed it (scope TASK): ASK holds the task until a person answers through it, INFORM shows it while the task carries on. Null for any other.")
    def task_role(self) -> enums.TaskBlokRole | None:
        role = row_of(self, models.MaterializedBlok).task_role
        return enums.TaskBlokRole(role) if role else None

    @strawberry_django.field(description="What it is opened on, by arg key (read under `@args.<key>`): set by the task that showed it. Empty when whoever shows it chooses.")
    def args(self) -> scalars.Args:
        return cast("scalars.Args", row_of(self, models.MaterializedBlok).args or {})

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
    resolved_locals: list[rtypes.ResolvedBlokLocal] = strawberry.field(description="The new content's locals as its tree resolves them, each with a kind (a left-out kind inferred from its default, the task argument it is passed to or the control bound to it; ANY when nothing tells): what an editor types the locals with before saving, a dry run included.")
