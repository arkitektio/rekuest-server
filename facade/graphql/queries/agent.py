from facade.filters import AgentFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Agent
import graphene
from lok import bounced


class AgentDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Agent.objects.get(id=id)

    class Meta:
        type = types.Agent
        operation = "agent"


class Agents(BalderQuery):
    class Meta:
        type = types.Agent
        list = True
        filter = AgentFilter
