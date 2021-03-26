from facade.models import AppProvider, Provider, Template
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced
from graphene.types.generic import GenericScalar



class Offer(BalderMutation):

    class Arguments:
        node = graphene.ID(required=True, description="The Node you offer to give an implementation for")
        params  = GenericScalar(required=False, description="Some additional Params for your offering")
        policy  = GenericScalar(required=False, description="Some additional Params for your offering")


    @bounced(only_jwt=True, required_scopes=["provider"])
    def mutate(root, info, node=None, name=None, params=None, policy=None):
        provider = AppProvider.objects.get(client_id=info.context.bounced.client_id, user=info.context.bounced.user)

        try:
            template = Template.objects.get(node=node, params=params)
        except:
            template = Template.objects.create(
                node_id=node,
                params=params,
                provider=provider,
            )
        # We check ids because AppProvider is not Provider subclass
        assert template.provider.id == provider.id, "Template cannot be offered because it already existed on another Provider, considering Implementing it differently or copy that implementation!"
        template.policy = policy
        template.save()
        return template
        


    class Meta:
        type = types.Template