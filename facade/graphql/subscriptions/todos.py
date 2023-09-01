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
        instance_id = graphene.String(
            description="The reference of this todos", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, instance_id=None):
        client = info.context.bounced.client
        user = info.context.bounced.user

        registry, _ = models.Registry.objects.update_or_create(
            user=user, client=client, defaults=dict(app=info.context.bounced.app)
        )
        agent, _ = models.Agent.objects.get_or_create(
            registry=registry, identifier=instance_id
        )
        return [f"todos_{agent.unique}"]

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


class MyTodosSubscription(BalderSubscription):
    class Arguments:
        pass

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, identifier=None):
        return [f"mytodos_{info.context.user.id}"]

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
        operation = "mytodos"
