from lok import bounced
from balder.types import BalderSubscription
from facade import types
import graphene


class NodeEvent(graphene.ObjectType):
    created = graphene.Field(types.Node)
    deleted = graphene.ID()
    updated = graphene.Field(types.Node)


class NodesEvent(BalderSubscription):
    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"nodes_user_{info.context.user.id}")
        return [f"nodes_user_{info.context.user.id}", "all_nodes"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]
        if action == "created":
            return {"created": data}
        if action == "updated":
            return {"updated": data}
        if action == "deleted":
            return {"deleted": data}

    class Meta:
        type = NodeEvent
        operation = "nodesEvent"


class NodeDetailEvent(BalderSubscription):
    class Arguments:
        id = graphene.ID(
            description="The Id of the node you are intereset in", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, id):
        print("NODE IS BEEING WATCHED BIIIITCH")
        return [f"node_{id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]
        print("Updated")

        if action == "updated":
            return data

    class Meta:
        type = types.Node
        operation = "nodeEvent"
