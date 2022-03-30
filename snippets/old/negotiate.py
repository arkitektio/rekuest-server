from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import (
    PostmanProtocol,
    PostmanSettings,
    Transcript,
)
from facade.models import Agent, Registry, Structure
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)


class Negotiate(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        version = graphene.String(required=False, description="Point type")

    class Meta:
        type = Transcript

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
    ):

        registry, _ = Registry.objects.update_or_create(
            app=info.context.bounced.app, user=info.context.bounced.user
        )

        agent_name = (
            info.context.bounced.app.name + info.context.bounced.user.username
            if info.context.bounced.user
            else info.context.bounced.app.name
        )

        registry, _ = Agent.objects.update_or_create(
            registry=registry,
            defaults={"name": agent_name},
        )

        transcript_dict = {
            "structures": Structure.objects.all(),
            "postman": PostmanSettings(type=PostmanProtocol.WEBSOCKET, kwargs={}),
        }

        return Transcript(**transcript_dict)
