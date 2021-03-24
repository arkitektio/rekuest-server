from facade.models import AppProvider, Provider, ServiceProvider, Template, Service
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced
from graphene.types.generic import GenericScalar
import socket


class CreateTemplate(BalderMutation):

    class Arguments:
        node = graphene.ID(required=True, description="The Node you offer to give an implementation for")
        params  = GenericScalar(required=False, description="Some additional Params for your offering")


    @bounced(only_jwt=True, required_scopes=["provider"])
    def mutate(root, info, node=None, name=None, params=None):
        provider, created = AppProvider.objects.update_or_create(client_id=info.context.bounced.client_id, user=info.context.bounced.user , defaults = {
            "name": info.context.bounced.app_name + " " + info.context.bounced.user.username
        })

        try:
            template = Template.objects.get(node=node, params=params)
        except:
            template = Template.objects.create(
                node_id=node,
                params=params,
                provider=provider.provider_ptr,
            )

        # We check ids because AppProvider is not Provider subclass
        assert template.provider.id == provider.id, "Template cannot be offered because it already existed on another Provider, considering Implementing it differently or copy that implementation!"
        return template
        


    class Meta:
        type = types.Template