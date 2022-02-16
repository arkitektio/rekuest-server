from facade.enums import ReservationStatus
from graphene.types.scalars import String
from lok import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class WaiterEvent(graphene.ObjectType):
    created = graphene.Field(types.Waiter)
    deleted = graphene.ID()
    updated = graphene.Field(types.Waiter)


class WaiterSubscription(BalderSubscription):
    WAITER_FOR_USERID = lambda id: f"waiter_user_{id}"


    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"waiter_user_{info.context.user.id}")
        return [WaiterSubscription.WAITER_FOR_USERID(info.context.user.id), "all_waiters"]

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
        type = WaiterEvent
        operation = "waiter"


