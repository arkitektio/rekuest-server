from facade.inputs import DefinitionInput
from facade import types
from facade.models import AppRepository, Agent, Structure
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
import inflection

logger = logging.getLogger(__name__)


class KickAgentReturn(graphene.ObjectType):
    id = graphene.String()


class KickAgent(BalderMutation):
    """Kick an agent (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="The id of the agent to delete",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        agent = Agent.objects.get(id=id)
        agent.kick()
        return agent

    class Meta:
        type = types.Agent


class BounceAgentReturn(graphene.ObjectType):
    id = graphene.String()


class BounceAgent(BalderMutation):
    """Kick an agent (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="The id of the agent to delete",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        agent = Agent.objects.get(id=id)
        agent.bounce()
        return agent

    class Meta:
        type = types.Agent



class BlockAgentReturn(graphene.ObjectType):
    id = graphene.String()


class BlockAgent(BalderMutation):
    """Kick an agent (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="The id of the agent to delete",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        agent = Agent.objects.get(id=id)
        agent.kick()
        agent.blocked = True
        agent.save()
        return agent

    class Meta:
        type = types.Agent


class DeleteAgentReturn(graphene.ObjectType):
    id = graphene.String()


class DeleteAgent(BalderMutation):
    """Deletes an agent (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="The id of the agent to delete",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        node = Agent.objects.get(id=id)
        node.delete()
        return {"id": id}

    class Meta:
        type = DeleteAgentReturn
