import uuid
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class Unprovide(graphene.ObjectType):
    reference = graphene.String()


class UnprovideMutation(BalderMutation):
    class Arguments:
        provision = graphene.String(
            description="The reference of the Provision you want to ruin"
        )
        reference = graphene.String(
            description="The reference of this cancellation",
            required=False,
        )

    class Meta:
        type = Unprovide
        operation = "unprovide"

    @bounced(only_jwt=True)
    def mutate(root, info, provision=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        return reference
