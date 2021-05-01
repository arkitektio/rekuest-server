from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade.types import Assignation
from facade import models
import graphene
import logging

logger = logging.getLogger(__name__)


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

        if action == "updated":
            return {"update": models.Assignation.objects.get(id=data)}
        if action == "created":
            return {"create": models.Assignation.objects.get(id=data)}
        if action == "ended":
            return {"ended": data}

        logger.error("error in payload")


    class Meta:
        type = AssignationsEvent
        operation = "myAssignationsEvent"


