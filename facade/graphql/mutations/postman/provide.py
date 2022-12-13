import uuid
from facade import types
from facade.models import Provision, Agent, Template
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class ProvideMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        template = graphene.ID(required=True)
        agent = graphene.ID(required=True)
        params = GenericScalar(description="Additional Params")

    class Meta:
        type = types.Provision
        operation = "provide"

    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template=None, params={}, agent=None):
        bounce = info.context.bounced

        temp = Template.objects.get(id=template)
        agent = Agent.objects.get(id=agent)

        assert agent.registry == temp.registry, "Agent and Template must be in the same Registry"

        pro = Provision.objects.create(
            **{
                "template": temp,
                "params": params,
                "agent": agent,
                "creator": bounce.user,
                "app": bounce.app,
            }
        )

        return pro
