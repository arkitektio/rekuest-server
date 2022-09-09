from facade.models import Reservation
from facade import types
import uuid
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.connection import rmq

logger = logging.getLogger(__name__)  #


class UnreserveResult(graphene.ObjectType):
    id = graphene.ID()


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

        res = Reservation.objects.get(id=id)
        res.delete()

        return res.id
