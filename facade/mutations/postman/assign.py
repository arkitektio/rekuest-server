from facade.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation, Reservation
from facade import types
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedAssignMessage
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class Assign(graphene.ObjectType):
    reference = graphene.String()



class AssignMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        reservation = graphene.String(description="The Base URL for the Datapoint you want to add", required=True)
        args = graphene.List(GenericScalar, description="The Base URL for the Datapoint you want to add", required=True)
        kwargs = GenericScalar(description="Additional Params")


    class Meta:
        type = types.Assignation
        operation = "assign"

    
    @bounced(only_jwt=True)
    def mutate(root, info, reservation=None, args = None, kwargs={}, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        ass = Assignation.objects.create(**{
            "reservation": Reservation.objects.get(reference=reservation),
            "args": args,
            "kwargs": kwargs,
            "context": {
                "roles": bounce.roles,
                "scopes": bounce.scopes,
                "user": bounce.user.id if bounce.user else None,
                "app": bounce.app.id if bounce.app else None
            },
            "reference": reference,
            "creator": bounce.user,
            "app": bounce.app,
            "callback": "not-set",
            "progress": "not-set"
        })

        MyAssignationsEvent.broadcast({"action": "created", "data": ass.id}, [f"assignations_user_{bounce.user.id}"])
        
        bounced = BouncedAssignMessage(data= {
            "reservation": reservation,
            "args": args,
            "kwargs": kwargs
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

        return ass
            

