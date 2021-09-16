from facade.enums import AssignationStatus
from graphene.types.scalars import String
from lok import bounced
from balder.types import BalderSubscription
from facade.types import Assignation
from facade import models
import graphene
import logging



logger = logging.getLogger(__name__)


class AssignationLogEvent(graphene.ObjectType):
    message = graphene.String()
    level = graphene.String()

class AssignationEvent(graphene.ObjectType):
    log = graphene.Field(AssignationLogEvent)

class AssignationEventSubscription(BalderSubscription):


    class Arguments:
        reference = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, reference=None, level=None):
        ass = models.Assignation.objects.get(reference=reference)
        assert ass.creator == info.context.bounced.user, "You cannot listen to a assignation that you have not created"
        return [f"assignation_{ass.reference}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]


        if action == "log":
            return {"log": data}

        if action == "update":
            return {"update": models.Assignation.objects.get(id=data)}

        print("error in payload")


    class Meta:
        type = AssignationEvent
        operation = "assignationEvent"



class AssignationsEvent(graphene.ObjectType):
    ended =  graphene.ID()
    update =  graphene.Field(Assignation)
    create = graphene.Field(Assignation)



class MyAssignationsEvent(BalderSubscription):


    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        return [f"assignations_user_{info.context.user.id}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        logger.error(payload)

        if action == "created":
            return {"create": models.Assignation.objects.get(id=data)}
        if action in [AssignationStatus.CANCELLED, AssignationStatus.DONE, AssignationStatus.RETURNED]:
            return {"ended": data}
        else:
            return {"update": models.Assignation.objects.get(id=data)}



    class Meta:
        type = AssignationsEvent
        operation = "myAssignationsEvent"

