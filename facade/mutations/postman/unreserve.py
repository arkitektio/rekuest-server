from facade.helpers import create_context_from_bounced

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


class Unreserve(graphene.ObjectType):
    reference = graphene.String()


class UnreserveMutation(BalderMutation):
    class Arguments:
        reservation = graphene.String(
            description="The reference of the Reservation you want to ruin"
        )
        reference = graphene.String(
            description="The reference of the Reservation you want to ruin",
            required=False,
        )

    class Meta:
        type = Unreserve
        operation = "unreserve"

    @bounced(only_jwt=True)
    def mutate(root, info, reservation=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        bounced = BouncedUnreserveMessage(
            data={
                "reservation": reservation,
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

        return reference
