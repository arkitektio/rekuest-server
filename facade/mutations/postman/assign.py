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
    ):
        reference = reference or str(uuid.uuid4())

        creator = info.context.bounced.user
        app = info.context.bounced.app

        res = Reservation.objects.get(id=reservation)

        ass = models.Assignation.objects.create(
            **{
                "reservation": res,
                "args": args,
                "kwargs": kwargs,
                "context": create_context_from_bounced(info.context.bounced),
                "reference": reference,
                "waiter": res.waiter,
                "creator": creator,
                "app": app,
                "callback": "not-set",
                "progress": "not-set",
            }
        )

        if creator:
            MyAssignationsEvent.broadcast(
                {"action": "created", "data": ass.id},
                [f"assignations_user_{creator.id}"],
            )

        """ 
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
        """

        # GatewayConsumer.send(bounced)

        return ass
