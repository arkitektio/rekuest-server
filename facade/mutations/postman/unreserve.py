from facade.enums import ReservationStatus
from facade.helpers import create_context_from_bounced
from facade.models import Reservation
from facade.subscriptions.waiter import WaiterSubscription
from facade import models, types
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnreserveMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class UnreserveMutation(BalderMutation):
    class Arguments:
        id = graphene.ID(
            description="The reference of the Reservation you want to ruin",
            required=True,
        )

    class Meta:
        type = types.Reservation
        operation = "unreserve"

    @bounced(only_jwt=True)
    def mutate(root, info, id=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        res = Reservation.objects.get(id=id)
        res.status = ReservationStatus.CANCELING
        res.save()

        bounced = BouncedUnreserveMessage(
            data={
                "reservation": id,
            },
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
