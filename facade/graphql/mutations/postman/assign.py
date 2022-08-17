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


class Assign(graphene.ObjectType):
    reference = graphene.String()


class AssignMutation(BalderMutation):
    class Arguments:
        reservation = graphene.ID(required=True)
        args = graphene.List(
            AnyInput,
            required=True,
        )
        kwargs = GenericScalar(description="Additional Params")
        reference = graphene.String(description="A reference")
        cached = graphene.Boolean(
            description="Should we allow cached results (only applicable if node was registered as pure)"
        )
        log = graphene.Boolean(
            description="Should we log intermediate resulst? (if also persist is true these will be persisted to the log system)"
        )
        mother = graphene.ID(description="If this task inherits from another task")

    class Meta:
        type = types.Assignation
        operation = "assign"

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        reservation=None,
        args=[],
        kwargs={},
        reference=None,
        cached=True,
    ):
        reference = reference or str(uuid.uuid4())

        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = Registry.objects.get_or_create(user=creator, app=app)

        creator = info.context.bounced.user
        app = info.context.bounced.app

        res = models.Reservation.objects.get(id=reservation)
        if res.status != ReservationStatus.ACTIVE:
            raise Exception("Cannot assign. Reservation is currently inactive!")

        ass = models.Assignation.objects.create(
            **{
                "reservation": res,
                "args": args,
                "kwargs": kwargs,
                "creator": creator,
                "status": AssignationStatus.ASSIGNED,
            }
        )

        forward = [
            AssignHareMessage(
                queue=ass.reservation.queue,
                reservation=ass.reservation.id,
                assignation=ass.id,
                args=ass.args,
                kwargs=ass.kwargs,
            )
        ]

        logger.error(forward[0].dict())

        for forward_res in forward:
            rmq.publish(forward_res.queue, forward_res.to_message())

        # GatewayConsumer.send(bounced)

        return ass
