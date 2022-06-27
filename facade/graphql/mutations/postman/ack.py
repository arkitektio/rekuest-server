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
        assignation = graphene.ID(required=True)

    class Meta:
        type = types.Assignation
        operation = "ack"

    @bounced(only_jwt=True)
    def mutate(root, info, assignation=None):

        ass = Assignation.objects.get(id=assignation)
        ass.status = AssignationStatus.ACKNOWLEDGED
        ass.save()

        return ass
