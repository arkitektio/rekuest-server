from facade.enums import ReservationStatus
from facade.models import Reservation
from facade import types
import uuid
from facade import types
from balder.types import BalderMutation
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


        return res
