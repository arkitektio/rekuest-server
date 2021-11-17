from balder.types.mutation.base import BalderMutation
from facade.enums import AgentStatus
from facade.models import Agent
from lok import bounced
import graphene


class ResetAgentsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetAgents(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        pass

    class Meta:
        type = ResetAgentsReturn

    @bounced(anonymous=True)
    def mutate(root, info, url=None, name=None):

        for agent in Agent.objects.all():
            agent.status = AgentStatus.VANILLA
            agent.save()

        return {"ok": True}
