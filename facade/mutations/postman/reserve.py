from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from facade import types
from facade.models import  Reservation
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class ReserveMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        node = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        template = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        reference = graphene.String(description="The Base URL for the Datapoint you want to add", required=False)
        params = GenericScalar(description="Additional Params")


    class Meta:
        type = types.Reservation
        operation = "reserve"

    
    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template = None, params={}, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        res = Reservation.objects.create(**{
            "node_id": node,
            "template_id": template,
            "params": params,
            "reference": reference,
            "creator": bounce.user,
            "callback": "not-set",
            "progress": "not-set"
        })

        
        bounced = BouncedReserveMessage(data= {
            "node": node,
            "template": template,
            "params": params
        }, meta= {
            "reference": reference,
            "extensions": {
                "callback": "not-set",
                "progress": "not-set",
            },
            "token": {
                "roles": bounce.roles,
                "scopes": bounce.scopes,
                "user": bounce.user.id if bounce.user else None
            }
        })

        GatewayConsumer.send(bounced)

        return res
            

