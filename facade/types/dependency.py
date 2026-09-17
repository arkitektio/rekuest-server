"""Dependencies, resolutions and agent/implementation mappings."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List

import strawberry
import strawberry_django

from facade import filters, loaders, models
from kante.types import Info
from facade.types.demand import ActionDependencyModel, StateDependencyModel
from facade.types.base import build_prescoped_queryset


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
        return [ActionDependencyModel(**d.model_dump()) for d in self.get_action_dependencies()]

    @strawberry_django.field(description="The named state requirements of this dependency.")
    def state_dependencies(self) -> list["StateDependency"]:
        return [StateDependencyModel(**d.model_dump()) for d in self.get_state_dependencies()]

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
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
    def get_queryset(cls, queryset, info, **kwargs):
        return build_prescoped_queryset(info, queryset, field="organization")


@strawberry.type
class ImplementationMapping:
    _key: strawberry.Private[str]
    _value: strawberry.Private[Dict[str, Any]]

    @strawberry_django.field(description="Get the key of the implementation mapping.")
    def key(self) -> str:
        return self._key

    @strawberry_django.field(description="Get the key of the implementation mapping.")
    async def implementation(self, info: Info) -> Implementation:
        return await loaders.implementation_loader(info).load(self._value.get("implementation"))

    @strawberry_django.field(description="Get the key of the implementation mapping.")
    def resolved_dependencies(self) -> list["ResolvedAgentDependency"]:
        dependencies: list[ResolvedAgentDependency] = []
        for value in self._value.get("dependencies"):
            dependencies.append(ResolvedAgentDependency(_key=value.get("key"), _value=value.get("value")))
        return dependencies


@strawberry.type
class AgentMapping:
    _value: strawberry.Private[Dict[str, Any]]

    @strawberry_django.field(description="Get the agent's name from the mapping.")
    async def agent(self, info: Info) -> Agent:
        return await loaders.agent_loader(info).load(self._value.get("agent"))

    @strawberry_django.field(description="Get the agent's ID from the mapping.")
    def agent_id(self) -> str:
        return self._value.get("agent")

    @strawberry_django.field(description="Get a specific argument by key.")
    def mapped_implementations(self) -> list[ImplementationMapping]:
        mappings: list[ImplementationMapping] = []
        for key, value in self._value.get("actions").items():
            mappings.append(ImplementationMapping(_key=key, _value=value))

        return mappings


@strawberry.type
class ResolvedAgentDependency:
    _key: strawberry.Private[str]
    _value: strawberry.Private[List[Dict[str, Any]]]

    @strawberry_django.field(description="Get a specific argument by key.")
    def values(self) -> str | None:
        return str(self._value)

    @strawberry_django.field(description="Get a specific argument by key.")
    def mapped_agents(self) -> list[AgentMapping]:
        mappings: list[AgentMapping] = []
        for value in self._value:
            mappings.append(AgentMapping(_value=value))

        return mappings

    @strawberry_django.field(description="Get the key of the resolved dependency.")
    def key(self) -> str:
        return self._key
