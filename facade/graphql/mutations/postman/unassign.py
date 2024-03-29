from facade.models import Assignation
import uuid
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.connection import pikaconnection

logger = logging.getLogger(__name__)  #


class UnassignMutation(BalderMutation):
    class Arguments:
        assignation = graphene.ID(
            description="The reference of the Assignation you want to ruin"
        )
        reference = graphene.String(
            description="An identifier you want this unassignation to go buy",
            required=False,
        )

    class Meta:
        type = types.Assignation
        operation = "unassign"

    @bounced(only_jwt=True)
    def mutate(root, info, assignation=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        assignation = Assignation.objects.get(id=assignation)

        assignation, forwards = assignation.unassign()

        for forward_res in forwards:
            pikaconnection.publish(forward_res.queue, forward_res.to_message())

        return assignation
