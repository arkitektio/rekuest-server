from graphene.types.scalars import String
from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade.types import Assignation
from facade import models
import graphene


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


