from facade.enums import AssignationStatus
from facade.graphql.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class AcknowledgeMutation(BalderMutation):
    class Arguments:
        assignation = graphene.String(required=True)

    class Meta:
        type = types.Assignation
        operation = "ack"

    @bounced(only_jwt=True)
    def mutate(root, info, assignation=None):
        bounce = info.context.bounced

        ass = Assignation.objects.get(reference=assignation)
        ass.status = AssignationStatus.ACKNOWLEDGED
        ass.save()

        MyAssignationsEvent.broadcast(
            {"action": "updated", "data": ass.id},
            [f"assignations_user_{bounce.user.id}"],
        )

        return ass
