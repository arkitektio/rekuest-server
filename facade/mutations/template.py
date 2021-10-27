from facade.models import Provider, Template
from facade import types
from balder.types import BalderMutation
import graphene
from lok import bounced
from graphene.types.generic import GenericScalar
import socket


class CreateTemplate(BalderMutation):

    class Arguments:
        node = graphene.ID(required=True, description="The Node you offer to give an implementation for")
        extensions = graphene.List(graphene.String, description="Desired Extensions", required=False)
        version = graphene.String( description="Desired Extensions", required=False)
        params  = GenericScalar(required=False, description="Some additional Params for your offering")
        policy  = GenericScalar(required=False, description="Some additional Params for your offering")


    @bounced(only_jwt=True)
    def mutate(root, info, node=None, name=None, params=None, policy = None, extensions = [], version= "main"):
        provider = Provider.objects.get(app=info.context.bounced.app, user=info.context.bounced.user)

        try:
            template = Template.objects.get(node=node, params=params, provider=provider)
            template.extensions = extensions
            template.version = version
            template.save()
        except:
            template = Template.objects.create(
                node_id=node,
                params=params,
                provider=provider,
                extensions = extensions,
                version = version
            )

        # We check ids because AppProvider is not Provider subclass
        return template
        


    class Meta:
        type = types.Template