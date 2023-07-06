from facade.inputs import MessageInput
from facade.enums import AssignationStatus, ReservationStatus
from facade.models import Registry, Reservation, Waiter
from facade.scalars import AnyInput
from facade import types, models
import uuid
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging
from hare.carrots import *
from hare.connection import rmq

logger = logging.getLogger(__name__)  #


class Tell(graphene.ObjectType):
    reference = graphene.String()


class TellMutation(BalderMutation):
    class Arguments:
        reservation = graphene.ID(required=True)
        message = graphene.Argument(MessageInput, required=True)

    class Meta:
        type = Tell
        operation = "tell"

    @bounced(only_jwt=True)
    def mutate(
        root,
        reservation,
        message,
    ):
        reference = reference or str(uuid.uuid4())


        # for forward_res in forward:
        #     rmq.publish(forward_res.queue, forward_res.to_message())


        return ass
