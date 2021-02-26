from facade.models import Provider, Template
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced
from graphene.types.generic import GenericScalar



class Offer(BalderMutation):

    class Arguments:
        node = graphene.ID(required=True, description="The Node you offer to give an implementation for")
        params  = GenericScalar(required=False, description="Some additional Params for your offering")


    @bounced(only_jwt=True)
    def mutate(root, info, node=None, name=None, params=None):
        provider = Provider.objects.get(app=info.context.auth.client_id, user=info.context.user)

        try:
            template = Template.objects.get(node=node, params=params)
        except:
            template = Template.objects.create(
                node_id=node,
                params=params,
                provider=provider,
            )

        assert template.provider == provider, "Template cannot be offered because it already existed on another Provider, considering Implementing it differently or copy that implementation!"
        return template
        


    class Meta:
        type = types.Template