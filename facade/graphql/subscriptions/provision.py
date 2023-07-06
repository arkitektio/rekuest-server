from facade.enums import LogLevel, ProvisionStatus
from lok import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class ProvisionLogEvent(graphene.ObjectType):
    message = graphene.String()
    level = graphene.String()


class ProvisionEvent(graphene.ObjectType):
    log = graphene.Field(ProvisionLogEvent)


class ProvisionSubscription(BalderSubscription):
    class Arguments:
        id = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")

    @classmethod
    def send_log(cls, groups, message, level=LogLevel.INFO):
        cls.broadcast(
            {"action": "log", "data": {"message": message, "level": level}}, groups
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, id, level=None):
        provision = models.Provision.objects.get(id=id)
        return [f"provision_{provision.id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "log":
            return {"log": data}

    class Meta:
        type = ProvisionEvent
        operation = "provision"


class ProvisionsEvent(graphene.ObjectType):
    delete = graphene.ID()
    update = graphene.Field(types.Provision)
    create = graphene.Field(types.Provision)


class ProvisionsSubscription(BalderSubscription):
    class Arguments:
        identifier = graphene.String(
            description="The reference of this waiter", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, identifier, **kwargs):
        client = info.context.bounced.client
        user = info.context.bounced.user

       
        registry, _ = models.Registry.objects.update_or_create(user=user, client=client, defaults=dict(app=info.context.bounced.app))
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=identifier
        )
        return [f"provisions_{waiter.unique}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "create":
            return {"create": data}
        else:
            return {"update": data}

    class Meta:
        type = ProvisionsEvent
        operation = "provisions"


class MyProvisionsSubscription(BalderSubscription):
    class Arguments:
        pass

    @bounced(only_jwt=True)
    def subscribe(root, info, identifier, **kwargs):
        return [f"myprovisions_{info.context.user.id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "create":
            return {"create": data}
        else:
            return {"update": data}

    class Meta:
        type = ProvisionsEvent
        operation = "myprovisions"
