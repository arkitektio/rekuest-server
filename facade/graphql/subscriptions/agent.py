import logging
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
        return [f"agents_user_{info.context.user.id}", "all_agents"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]
        logging.error(f"{action} {data}")

        if action == "create":
            return {"created": data}
        if action == "update":
            return {"updated": data}
        if action == "delete":
            return {"deleted": data}

    class Meta:
        type = AgentEvent
        operation = "agentsEvent"
