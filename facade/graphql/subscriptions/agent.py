from lok import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class AgentEvent(graphene.ObjectType):
    created = graphene.Field(types.Agent)
    deleted = graphene.ID()
    updated = graphene.Field(types.Agent)


class AgentsEvent(BalderSubscription):
    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"agents_user_{info.context.user.id}")
        return [f"agents_user_{info.context.user.id}", "all_agents"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "created":
            return {"created": models.Agent.objects.get(id=data)}
        if action == "updated":
            return {"updated": models.Agent.objects.get(id=data)}
        if action == "deleted":
            return {"deleted": data}

    class Meta:
        type = AgentEvent
        operation = "agentsEvent"
