from facade.helpers import create_context_from_bounced
from facade.subscriptions.reservation import MyReservationsEvent
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from facade import types
from facade.enums import ReservationStatus
from facade.models import  Reservation
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)#


class ReserveMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        node = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        template = graphene.ID(description="The Base URL for the Datapoint you want to add", required=False)
        reference = graphene.String(description="The Base URL for the Datapoint you want to add", required=False)
        title = graphene.String(description="A cleartext shorthand title", required=False)
        params = graphene.Argument(types.ReserveParamsInput, description="Additional Params", required=False)


    class Meta:
        type = types.Reservation
        operation = "reserve"

    
    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template = None, params={}, title=None, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        res = Reservation.objects.create(**{
            "node_id": node,
            "template_id": template,
            "params": params,
            "status": ReservationStatus.ROUTING,
            "title": title,
            "context": create_context_from_bounced(bounce),
            "reference": reference,
            "creator": bounce.user,
            "app": bounce.app,
            "callback": "not-set",
            "progress": "not-set"
        })


        MyReservationsEvent.broadcast({"action": "created", "data": res.id}, [f"reservations_user_{bounce.user.id}"])


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
            "context": create_context_from_bounced(bounce)
        })

        GatewayConsumer.send(bounced)

        return res
            

