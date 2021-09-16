from facade.helpers import create_context_from_bounced
from facade.subscriptions import provision
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnprovideMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
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
            "context": create_context_from_bounced(bounce)
        })

        GatewayConsumer.send(bounced)

        return reference
            

