from ast import Delete
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


class AssignationSubscription(BalderSubscription):
    class Arguments:
        id = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, id, level=None):
        ass = models.Assignation.objects.get(id=id)
        return [f"assignation_{ass.id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "log":
            return {"log": data}

        if action == "update":
            return {"update": models.Assignation.objects.get(id=data)}

    class Meta:
        type = AssignationEvent
        operation = "assignation"


class AssignationsEvent(graphene.ObjectType):
    delete = graphene.ID()
    update = graphene.Field(Assignation)
    create = graphene.Field(Assignation)


class MyRequestsSubscription(BalderSubscription):
    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        return [f"myrequests_{info.context.user.id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        logger.error(payload)

        if action == "create":
            return {"create": data}
        else:
            return {"update": data}

    class Meta:
        type = AssignationsEvent
        operation = "myrequests"


class RequestsSubscription(BalderSubscription):
    class Arguments:
        instance_id = graphene.String(
            description="The log level for alterations", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, instance_id, **kwargs):
        client = info.context.bounced.client
        user = info.context.bounced.user

        registry, _ = models.Registry.objects.update_or_create(
            user=user, client=client, defaults=dict(app=info.context.bounced.app)
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
        )
        return [f"requests_{waiter.unique}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        logger.error(payload)

        if action == "create":
            return {"create": data}
        else:
            return {"update": data}

    class Meta:
        type = AssignationsEvent
        operation = "requests"
