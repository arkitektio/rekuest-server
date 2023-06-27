from facade.filters import AgentFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Agent
import graphene
from lok import bounced


class AgentDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod", required=False)
        client = graphene.ID(description="The query pod", required=False)
        sub = graphene.ID(description="The query pod", required=False)
        instance = graphene.ID(description="The query pod", required=False, default_value="main")

    @bounced(anonymous=True)
    def resolve(root, info, id=None, client=None, sub=None, instance=None):
        if id:
            return Agent.objects.get(id=id)
        if client:
            agents =  Agent.objects.filter(registry__client__client_id=client)
            print(agents.all())
            if sub :
                agents = agents.filter(registry__client__user__sub=sub)
            if instance:
                agents = agents.filter(instance_id=instance)

            count = agents.count()
            assert count == 1, f"There should be only one agent per client, sub and instance. Found {count}"
            return agents.first()
        else:
            raise Exception("You need to provide either id or client")


    class Meta:
        type = types.Agent
        operation = "agent"


class Agents(BalderQuery):
    class Meta:
        type = types.Agent
        list = True
        filter = AgentFilter


class MyAgents(BalderQuery):
    class Meta:
        type = types.Agent
        personal = "registry__user"
        list = True
        filter = AgentFilter
        operation = "myagents"
