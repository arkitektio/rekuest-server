import datetime

from kante.types import Info
import strawberry
from facade import models, enums
from typing import AsyncGenerator
from facade.channels import agent_updated_channel


@strawberry.type(description="Slim, non-traversable snapshot of an agent for change feeds.")
class AgentChange:
    id: strawberry.ID
    name: str
    kind: enums.AgentKind
    connected: bool
    blocked: bool
    last_seen: datetime.datetime | None
    client: strawberry.ID
    user: strawberry.ID
    organization: strawberry.ID
    app: strawberry.ID
    release: strawberry.ID

    @classmethod
    def from_model(cls, a: models.Agent) -> "AgentChange":
        return cls(
            id=strawberry.ID(str(a.id)),
            name=a.display_name or a.name,
            kind=enums.AgentKind(a.kind),
            connected=a.connected,
            blocked=a.blocked,
            last_seen=a.last_seen,
            client=strawberry.ID(str(a.client_id)),
            user=strawberry.ID(str(a.user_id)),
            organization=strawberry.ID(str(a.organization_id)),
            app=strawberry.ID(str(a.app_id)),
            release=strawberry.ID(str(a.release_id)),
        )


@strawberry.type
class AgentChangeEvent:
    create: AgentChange | None = None
    update: AgentChange | None = None
    delete: strawberry.ID | None = None


async def agents(
    self,
    info: Info,
) -> AsyncGenerator[AgentChangeEvent, None]:
    """Subscribe to slim agent changes across the whole organization."""

    organization = info.context.request.organization

    async for message in agent_updated_channel.listen(info.context, [f"agents_for_{organization.id}"]):
        if message.create:
            yield AgentChangeEvent(create=AgentChange.from_model(await models.Agent.objects.aget(id=message.create)))
        elif message.update:
            yield AgentChangeEvent(update=AgentChange.from_model(await models.Agent.objects.aget(id=message.update)))
        elif message.delete:
            yield AgentChangeEvent(delete=strawberry.ID(str(message.delete)))
        else:
            raise ValueError("Unknown message type")
