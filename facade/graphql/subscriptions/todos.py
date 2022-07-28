from lok import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class TodoEvent(graphene.ObjectType):
    update = graphene.Field(types.Assignation)
    delete = graphene.Field(graphene.ID)
    create = graphene.Field(types.Assignation)


class TodosSubscription(BalderSubscription):
    class Arguments:
        identifier = graphene.ID(
            description="The reference of this todos", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, identifier=None):
        registry, _ = models.Registry.objects.get_or_create(
            user=info.context.bounced.user, app=info.context.bounced.app
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=identifier
        )
        return [f"todos_{waiter.unique}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "delete":
            return {"delete": data}

        if action == "update":
            return {"update": data}

        if action == "create":
            return {"create": data}

    class Meta:
        type = TodoEvent
        operation = "todos"
