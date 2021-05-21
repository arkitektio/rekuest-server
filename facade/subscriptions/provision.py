from facade.enums import LogLevel, ProvisionStatus
from graphene.types.scalars import String
from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class ProvisionLogEvent(graphene.ObjectType):
    message = graphene.String()
    level = graphene.String()


class ProvisionEvent(graphene.ObjectType):
    log = graphene.Field(ProvisionLogEvent)


class ProvisionEventSubscription(BalderSubscription):

    class Arguments:
        reference = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")


    @classmethod
    def send_log(cls, groups, message, level=LogLevel.INFO):
        cls.broadcast({"action": "log", "data": {"message": message, "level": level}}, groups)

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, reference=None, level=None):
        provision = models.Provision.objects.get(reference=reference)
        assert provision.creator == info.context.bounced.user, "You cannot listen to a reservation that you have not created"
        return [f"provision_{provision.reference}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        print(payload)

        if action == "log":
            return {"log": data}

        if action == "update":
            return {"log": models.Provision.objects.get(id=data)}

        print("error in payload")


    class Meta:
        type = ProvisionEvent
        operation = "provisionEvent"


class ProvisionsEvent(graphene.ObjectType):
    ended =  graphene.ID()
    update =  graphene.Field(types.Provision)
    create = graphene.Field(types.Provision)



class MyProvisionsEvent(BalderSubscription):


    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"provisions_user_{info.context.user.id}")
        return [f"provisions_user_{info.context.user.id}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "created":
            return {"create": models.Provision.objects.get(id=data)}
        if action in [ProvisionStatus.CANCELLED, ProvisionStatus.ENDED]:
            return {"ended": data}
        else:
            return {"update": models.Provision.objects.get(id=data)}



    class Meta:
        type = ProvisionsEvent
        operation = "myProvisionsEvent"



