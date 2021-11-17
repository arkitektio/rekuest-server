from facade.helpers import create_context_from_bounced
from facade.subscriptions.reservation import MyReservationsEvent
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from facade import types
from facade.enums import ReservationStatus
from facade.models import Reservation
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class ReserveMutation(BalderMutation):
    class Arguments:
        node = graphene.ID(required=False)
        template = graphene.ID(required=False)
        reference = graphene.String(required=False)
        title = graphene.String(required=False)
        params = graphene.Argument(
            types.ReserveParamsInput, description="Additional Params", required=False
        )
        persist = graphene.Boolean(
            default_value=True, description="Additional Params", required=False
        )

    class Meta:
        type = types.Reservation
        operation = "reserve"

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        node=None,
        template=None,
        params={},
        title=None,
        reference=None,
        persist=True,
    ):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        res = Reservation.objects.create(
            **{
                "node_id": node,
                "template_id": template,
                "params": params,
                "status": ReservationStatus.ROUTING,
                "title": title,
                "context": create_context_from_bounced(bounce),
                "extensions": {"persist": persist},
                "reference": reference,
                "creator": bounce.user,
                "app": bounce.app,
                "callback": "not-set",
                "progress": "not-set",
            }
        )

        MyReservationsEvent.broadcast(
            {"action": "created", "data": res.id},
            [f"reservations_user_{bounce.user.id}"],
        )

        bounced = BouncedReserveMessage(
            data={"node": node, "template": template, "params": params},
            meta={
                "reference": reference,
                "extensions": {
                    "callback": "not-set",
                    "progress": "not-set",
                },
                "context": create_context_from_bounced(bounce),
            },
        )

        GatewayConsumer.send(bounced)

        return res
