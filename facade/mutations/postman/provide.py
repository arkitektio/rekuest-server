from facade.consumers.postman import create_context_from_bounced
from facade.subscriptions.provision import MyProvisionsEvent
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedProvideMessage
from facade import types
from facade.models import  Provision, Reservation
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class ProvideMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        node = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        template = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        reference = graphene.String(description="The Base URL for the Datapoint you want to add", required=False)
        params = GenericScalar(description="Additional Params")


    class Meta:
        type = types.Provision
        operation = "provide"

    
    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template = None, params={}, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        pro = Provision.objects.create(**{
            "node_id": node,
            "template_id": template,
            "params": params,
            "context": create_context_from_bounced(bounce),
            "reference": reference,
            "creator": bounce.user,
            "app": bounce.app,
            "callback": "not-set",
            "progress": "not-set"
        })


        MyProvisionsEvent.broadcast({"action": "created", "data": pro.id}, [f"provisions_user_{bounce.user.id}"])


        bounced = BouncedProvideMessage(data= {
            "node": node,
            "template": template,
            "params": params
        }, meta= {
            "reference": reference,
            "extensions": {
                "callback": "not-set",
                "progress": "not-set",
            },
            "context": create_context_from_bounced(bounce)
        })

        GatewayConsumer.send(bounced)

        return pro
            

