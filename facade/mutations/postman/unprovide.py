from facade.subscriptions import provision
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnprovideMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class Unprovide(graphene.ObjectType):
    reference = graphene.String()



class UnprovideMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        provision = graphene.String(description="The reference of the Reservation you want to ruin")
        reference = graphene.String(description="The reference of the Reservation you want to ruin", required=False)


    class Meta:
        type = Unprovide
        operation = "unprovide"

    
    @bounced(only_jwt=True)
    def mutate(root, info, provision=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced
        
        bounced = BouncedUnprovideMessage(data= {
            "provision": provision,
        }, meta= {
            "reference": reference,
            "extensions": {
                "callback": "not-set",
                "progress": "not-set",
            },
            "token": {
                "roles": bounce.roles,
                "scopes": bounce.scopes,
                "user": bounce.user.id if bounce.user else None,
                "app": bounce.app.id if bounce.app else None
            }
        })

        GatewayConsumer.send(bounced)

        return reference
            

