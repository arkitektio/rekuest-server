from facade.models import Provider
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced

class Serve(BalderMutation):

    class Arguments:
        name = graphene.String(required=True, description="The unique name you shall henve be known for")


    @bounced(only_jwt=True)
    def mutate(root, info, name):
        provider , _ = Provider.objects.update_or_create(app=info.context.auth.client_id, user=info.context.user, defaults= {"name": name})
        return provider


    class Meta:
        type = types.Provider