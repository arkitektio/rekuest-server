from facade.consumers.postman import create_context_from_bounced
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnassignMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class Unassign(graphene.ObjectType):
    reference = graphene.String()



class UnassignMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        assignation = graphene.String(description="The reference of the Assignation you want to ruin")
        reference = graphene.String(description="An identifier you want this unassignation to go buy", required=False)


    class Meta:
        type = Unassign
        operation = "unassign"

    
    @bounced(only_jwt=True)
    def mutate(root, info, assignation=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced
        
        bounced = BouncedUnassignMessage(data= {
            "assignation": assignation,
        }, meta= {
            "reference": reference,
            "extensions": {
                "callback": "not-set",
                "progress": "not-set",
                "persist": True,
            },
            "context": create_context_from_bounced(bounce)
        })

        GatewayConsumer.send(bounced)

        return reference
            

