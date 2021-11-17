from facade.helpers import create_context_from_bounced
from facade.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation, Reservation
from facade import types
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
        reservation = graphene.String(required=True)
        args = graphene.List(
            GenericScalar,
            required=True,
        )
        kwargs = GenericScalar(description="Additional Params")

    class Meta:
        type = types.Assignation
        operation = "assign"

    @bounced(only_jwt=True)
    def mutate(root, info, reservation=None, args=None, kwargs={}, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        ass = Assignation.objects.create(
            **{
                "reservation": Reservation.objects.get(reference=reservation),
                "args": args,
                "kwargs": kwargs,
                "context": create_context_from_bounced(bounce),
                "reference": reference,
                "creator": bounce.user,
                "app": bounce.app,
                "callback": "not-set",
                "progress": "not-set",
            }
        )

        MyAssignationsEvent.broadcast(
            {"action": "created", "data": ass.id},
            [f"assignations_user_{bounce.user.id}"],
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

        GatewayConsumer.send(bounced)

        return ass
