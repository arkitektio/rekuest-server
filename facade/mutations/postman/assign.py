from facade.enums import ReservationStatus
from facade.helpers import create_context_from_bounced
from facade.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation, Reservation
from facade import types, models
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedAssignMessage
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class Assign(graphene.ObjectType):
    reference = graphene.String()


class AssignMutation(BalderMutation):
    class Arguments:
        reservation = graphene.ID(required=True)
        args = graphene.List(
            GenericScalar,
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

        res = Reservation.objects.get(id=reservation)
        if res.status != ReservationStatus.ACTIVE:
            raise Exception("Cannot assign. Reservation is currently inactive!")

        ass = models.Assignation.objects.create(
            **{
                "reservation": res,
                "args": args,
                "kwargs": kwargs,
                "reference": reference,
                "waiter": res.waiter,
                "creator": creator,
                "app": app,
            }
        )

        bounced = BouncedAssignMessage(
            data={"reservation": reservation, "args": args, "kwargs": kwargs},
            meta={
                "reference": reference,
                "extensions": {
                    "callback": "not-set",
                    "progress": "not-set",
                    "persist": True,
                },
                "context": create_context_from_bounced(bounce),
            },
        )

        # GatewayConsumer.send(bounced)

        return ass
