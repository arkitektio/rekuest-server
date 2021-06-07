from facade.enums import LogLevel, ProvisionStatus
from graphene.types.scalars import String
from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class ProviderEvent(graphene.ObjectType):
    started = graphene.Field(types.Provider)
    ended = graphene.ID()
    updated = graphene.Field(types.Provider)


class ProvidersEvent(BalderSubscription):

    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"providers_user_{info.context.user.id}")
        return [f"providers_user_{info.context.user.id}","all_providers"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "started":
            return {"started": models.Provider.objects.get(id=data)}
        if action == "ended":
            return {"ended": data}
        else:
            return {"updated": models.Provider.objects.get(id=data)}



    class Meta:
        type = ProviderEvent
        operation = "providersEvent"
