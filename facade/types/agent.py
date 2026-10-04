"""Agent and lock types."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Optional, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import enums, filters, liveness, models
from facade.types.base import build_prescoped_queryset, row_of

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.auth import App, Client, Device, Organization, Release, User
    from facade.types.blok import BlokAgentMapping
    from facade.types.implementation import Implementation
    from facade.types.schedule import Schedule
    from facade.types.session import Session
    from facade.types.shelve import MemoryShelve
    from facade.types.state import State
    from facade.types.task import Task
    from facade.types.threed import Placement
    from facade.types.trigger import Trigger


@strawberry_django.type(models.Lock, description="A resource of an agent that one of its tasks holds at a time: the agent takes it while an implementation that requires it runs.")
class Lock:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the lock.")
    key: str = strawberry_django.field(description="The lock's key, unique within its agent.")
    description: str | None = strawberry_django.field(description="What the lock guards.")
    agent: "Agent" = strawberry_django.field(description="The agent the lock belongs to.")
    held_by: Optional["Task"] = strawberry_django.field(field_name="hold_by", description="The task holding the lock right now, if any.")
    required_by: list["Implementation"] = strawberry_django.field(description="The implementations that take this lock while they run.")
    updated_at: datetime.datetime = strawberry_django.field(description="When the lock was last taken, released or redeclared.")

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Lock], info: Info, **kwargs: object) -> QuerySet[models.Lock]:
        return build_prescoped_queryset(info, queryset, field="agent__organization")


@strawberry_django.type(models.Agent, filters=filters.AgentFilter, ordering=filters.AgentOrder, pagination=True, description="Represents a compute agent that can execute implementations.")
class Agent:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the agent.")
    hash: str = strawberry_django.field(description="Hash representing the agent's definition for change detection.")
    client: "Client" = strawberry_django.field(description="The client (app instance) this agent runs as.")
    user: "User" = strawberry_django.field(description="The user this agent belongs to.")
    organization: "Organization" = strawberry_django.field(description="The organization this agent belongs to.")

    @strawberry_django.field(description="Device associated with the agent, via its client (if any).")
    def device(self, info: Info) -> Device | None:
        return self.client.device

    implementations: list["Implementation"] = strawberry_django.field(description="Implementations the agent can run.")
    locks: list["Lock"] = strawberry_django.field(description="The agent's locks, and which task holds each.")
    memory_shelve: Optional["MemoryShelve"] = strawberry_django.field(description="Agent's associated memory shelve.")
    last_seen: datetime.datetime | None = strawberry_django.field(description="Last timestamp this agent was seen.")
    installed_at: datetime.datetime = strawberry_django.field(description="When this agent first registered.")
    connected: bool = strawberry_django.field(description="Is the agent currently connected.")

    @strawberry_django.field(description="Agent name: the one a user gave it (updateAgent), else the one it declares.", only=["name", "display_name"])
    def name(self) -> str:
        row = row_of(self, models.Agent)
        return row.display_name or row.name

    declared_name: str = strawberry_django.field(field_name="name", description="The name the agent declares for itself, whatever a user calls it.")
    description: str | None = strawberry_django.field(description="What this agent is, in a sentence. Client-declared at registration; null for an agent that never declared one.")
    states: list["State"] = strawberry_django.field(description="Current and historical states associated with the agent.")
    kind: enums.AgentKind = strawberry_django.field(description="Kind of the agent.")
    hook_url: str | None = strawberry_django.field(description="Webhook URL for this Agent (only if webhook)", default=None)
    hook_url_secret: str | None = strawberry_django.field(description="Webhook URL secret for this Agent (only if webhook)", default=None)
    tasks: list["Task"] = strawberry_django.field(description="Tasks executed by this agent.")
    schedules: list["Schedule"] = strawberry_django.field(description="The schedules whose runs are pinned to this agent.")
    triggers: list["Trigger"] = strawberry_django.field(description="The triggers whose runs are pinned to this agent.")
    app: App = strawberry_django.field(description="The app this agent belongs to.")
    release: Release = strawberry_django.field(description="The release this agent belongs to.")
    placements: list["Placement"] = strawberry_django.field(description="Placements associated with this agent.")
    sessions: list["Session"] = strawberry_django.field(description="Sessions associated with this agent.")
    agent_mappings: list["BlokAgentMapping"] = strawberry_django.field(description="Blok mappings associated with this agent.")

    @strawberry_django.field(description="Fetch a specific implementation by interface.")
    def implementation(self, interface: str) -> Implementation | None:
        row = row_of(self, models.Agent)
        return cast("Implementation | None", row.implementations.filter(interface=interface).first())

    @strawberry_django.field(description="Determine if the agent is currently active based on last seen timestamp.")
    def active(self) -> bool:
        return liveness.agent_is_live(self.connected, self.last_seen)

    @strawberry_django.field(description="Check if this agent is pinned by the current user.")
    def pinned(self, info: Info) -> bool:
        row = row_of(self, models.Agent)
        user = info.context.request.user
        return row.pinned_by.filter(id=user.id).exists()

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Agent], info: Info, **kwargs: object) -> QuerySet[models.Agent]:
        return build_prescoped_queryset(info, queryset, field="organization")

    @strawberry_django.field(description="Get the count of implementations available on this agent.")
    def blocked(self) -> bool:
        return self.blocked
