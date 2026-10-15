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
from facade.types.base import build_prescoped_queryset, row_of
from facade.types.demand import ActionDependencyModel, StateDependencyModel
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.auth import User
    from facade.types.dashboard import DashboardPlacement, UICatalog
    from facade.types.demand import ActionDependency, StateDependency
    from facade.types.threed import Placement


def blok_hash(components: object, demo_state: object, context: str) -> str:
    """The hash of a blok's rendering: canonical JSON (sorted keys, no whitespace) of its parts."""
    canonical = json.dumps({"components": components, "demo_state": demo_state, "context": context}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@strawberry_django.type(models.Blok)
class Blok:
    id: strawberry.ID
    name: str
    description: str | None
    creator: User
    catalog: UICatalog
    materialized_bloks: list["MaterializedBlok"] = strawberry_django.field(
        description="Materialized bloks that are instances of this blok.",
    )
    dependencies: list["BlokDependency"] = strawberry_django.field(
        description="Dependencies that need to be resolved for this blok.",
    )

    @strawberry_django.field(description="The renderer for this blok's root.")
    def context(self) -> renums.ComponentContext:
        return renums.ComponentContext(self.context)

    for_model: str | None = strawberry_django.field(description="The model reference this blok decorates, if any.")
    for_node: str | None = strawberry_django.field(description="The node of `forModel` this blok attaches to; the model's root when null.")

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

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Blok], info: Info, **kwargs: object) -> QuerySet[models.Blok]:
        return build_prescoped_queryset(info, queryset, field="organization")


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
