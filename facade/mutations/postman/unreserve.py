from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnreserveMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class Unreserve(graphene.ObjectType):
    reference = graphene.String()



class UnreserveMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        reservation = graphene.String(description="The reference of the Reservation you want to ruin")
        reference = graphene.String(description="The reference of the Reservation you want to ruin", required=False)


    class Meta:
        type = Unreserve
        operation = "unreserve"

    
    @bounced(only_jwt=True)
    def mutate(root, info, reservation=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced
        
        bounced = BouncedUnreserveMessage(data= {
            "reservation": reservation,
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

        return reference
            

