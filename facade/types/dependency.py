"""Dependencies, resolutions and agent/implementation mappings."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, loaders, models
from facade.takt_api import BoundAgent, BoundImplementation, DependencyLevel, DependencyNote, ResolveAnswer
from facade.types.base import build_prescoped_queryset, row_of
from facade.types.demand import ActionDependencyModel, StateDependencyModel

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.agent import Agent
    from facade.types.auth import Organization, User
    from facade.types.demand import ActionDependency, StateDependency
    from facade.types.implementation import Implementation


@strawberry_django.type(models.Dependency, filters=filters.DependencyFilter, pagination=True, description="Represents a dependency between implementations and actions.")
class Dependency:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the dependency.")
    implementation: "Implementation" = strawberry_django.field(description="The implementation this dependency belongs to.")
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
        row = row_of(self, models.Dependency)
        return cast("list[ActionDependency]", [ActionDependencyModel(**d.model_dump()) for d in row.get_action_dependencies()])

    @strawberry_django.field(description="The named state requirements of this dependency.")
    def state_dependencies(self) -> list["StateDependency"]:
        row = row_of(self, models.Dependency)
        return cast("list[StateDependency]", [StateDependencyModel(**d.model_dump()) for d in row.get_state_dependencies()])

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Dependency], info: Info, **kwargs: object) -> QuerySet[models.Dependency]:
        return build_prescoped_queryset(info, queryset, field="implementation__action__organization")


@strawberry_django.type(models.ResolvedDependency, filters=filters.ResolvedDependencyFilter, pagination=True, description="Represents a dependency that has been resolved to a specific implementation.")
class ResolvedDependency:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the resolved dependency.")
    key: str = strawberry_django.field(description="The key of the resolved dependency.")
    resolution_key: str = strawberry_django.field(description="The resolution key associated with this resolved dependency.")
    dependency: "Dependency" = strawberry_django.field(description="The original dependency.")
    implementation: "Implementation" = strawberry_django.field(description="The implementation that resolves the dependency.")
    down_stream_resolution: "Resolution | None" = strawberry_django.field(description="Resolution for streaming data down to this dependency.")


@strawberry_django.type(models.Resolution, filters=filters.ResolutionFilter, pagination=True, description="Represents a resolution for a blok.")
class Resolution:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the resolution.")
    name: str = strawberry_django.field(description="Name of the resolution.")
    resolved_dependencies: list["ResolvedDependency"] = strawberry_django.field(description="List of resolved dependencies for this resolution.")
    implementation: "Implementation"
    resolved_at: datetime.datetime = strawberry_django.field(description="Timestamp when the resolution was created.")
    creator: User = strawberry_django.field(description="User who created the resolution.")
    organization: Organization = strawberry_django.field(description="Organization that owns this resolution.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Resolution], info: Info, **kwargs: object) -> QuerySet[models.Resolution]:
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry.type
class ImplementationMapping:
    _key: strawberry.Private[str]
    _value: strawberry.Private[BoundImplementation]

    @strawberry_django.field(description="Get the key of the implementation mapping.")
    def key(self) -> str:
        return self._key

    @strawberry_django.field(description="Get the key of the implementation mapping.")
    async def implementation(self, info: Info) -> Implementation:
        if self._value.implementation is None:
            raise ValueError(f"{self._key} is bound to no implementation")
        implementation = await loaders.implementation_loader(info).load(self._value.implementation)
        if implementation is None:
            raise ValueError(f"The implementation {self._value.implementation} no longer exists")
        return cast("Implementation", implementation)

    @strawberry_django.field(description="What the bound implementation's own dependencies resolve to: the level below.")
    def resolved_dependencies(self) -> list["ResolvedAgentDependency"]:
        return resolved_level(self._value, self._value.implementation)


@strawberry.type
class AgentMapping:
    _value: strawberry.Private[BoundAgent]

    @strawberry_django.field(description="Get the agent's name from the mapping.")
    async def agent(self, info: Info) -> Agent:
        agent = await loaders.agent_loader(info).load(self._value.agent)
        if agent is None:
            raise ValueError(f"The agent {self._value.agent} no longer exists")
        return cast("Agent", agent)

    @strawberry_django.field(description="Get the agent's ID from the mapping.")
    def agent_id(self) -> str:
        return self._value.agent

    @strawberry_django.field(description="Get a specific argument by key.")
    def mapped_implementations(self) -> list[ImplementationMapping]:
        return [ImplementationMapping(_key=key, _value=bound) for key, bound in self._value.actions.items()]


def resolved_level(level: DependencyLevel, implementation: str | int | None) -> list["ResolvedAgentDependency"]:
    """One level of a dependency tree, per dependency key.

    ``implementation`` is whose dependencies these are; ``meta`` is only there on a dry run.
    """
    return [ResolvedAgentDependency(_key=key, _value=agents or [], _implementation=implementation, _meta=level.meta.get(key)) for key, agents in level.dependencies.items()]


@strawberry.type
class ResolvedAgentDependency:
    _key: strawberry.Private[str]
    _value: strawberry.Private[list[BoundAgent]]
    _implementation: strawberry.Private[str | int | None] = None
    _meta: strawberry.Private[DependencyNote | None] = None

    @strawberry_django.field(description="The dependency as its implementation declares it, while it still declares it.")
    async def dependency(self) -> Dependency | None:
        declared = self._meta.dependency if self._meta is not None else None
        if declared is not None:
            return cast("Dependency | None", await models.Dependency.objects.filter(pk=declared).afirst())
        if self._implementation is None:
            return None
        return cast("Dependency | None", await models.Dependency.objects.filter(implementation_id=self._implementation, key=self._key).order_by("id").afirst())

    @strawberry_django.field(description="Why an assign would refuse this dependency as it is bound here. Only a dry run (dependencyTree) says so; null when it is met.")
    def unmet(self) -> str | None:
        return self._meta.unmet if self._meta is not None else None

    @strawberry_django.field(description="Get a specific argument by key.")
    def values(self) -> str | None:
        return str([agent.model_dump(mode="json") for agent in self._value])

    @strawberry_django.field(description="Get a specific argument by key.")
    def mapped_agents(self) -> list[AgentMapping]:
        return [AgentMapping(_value=agent) for agent in self._value]

    @strawberry_django.field(description="Get the key of the resolved dependency.")
    def key(self) -> str:
        return self._key


@strawberry.type(description="What assigning an implementation would bind: its dependency tree, resolved without assigning.")
class DependencyTree:
    _value: strawberry.Private[ResolveAnswer]
    _implementation: strawberry.Private[str]

    @strawberry_django.field(description="The implementation's dependencies, each with the agents it would bind and, below those, their own.")
    def dependencies(self) -> list[ResolvedAgentDependency]:
        return resolved_level(self._value, self._implementation)

    @strawberry_django.field(description="Whether an assign with these overwrites would go through: nothing in the tree is unmet.")
    def satisfied(self) -> bool:
        return self._value.satisfied
