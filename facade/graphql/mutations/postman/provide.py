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
        params = GenericScalar(description="Additional Params")

    class Meta:
        type = types.Provision
        operation = "provide"

    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template=None, params={}):
        bounce = info.context.bounced

        temp = Template.objects.get(id=template)


        pro = Provision.objects.create(
            **{
                "template": temp,
                "params": params,
                "agent": temp.agent,
                "creator": bounce.user,
                "app": bounce.app,
            }
        )

        return pro
