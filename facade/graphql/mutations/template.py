from facade.models import Registry, Template
from facade import types
from balder.types import BalderMutation
import graphene
from lok import bounced
from graphene.types.generic import GenericScalar


class CreateTemplate(BalderMutation):
    class Arguments:
        node = graphene.ID(
            required=True,
            description="The Node you offer to give an implementation for",
        )
        extensions = graphene.List(
            graphene.String, description="Desired Extensions", required=False
        )
        version = graphene.String(description="Desired Extensions", required=False)
        params = GenericScalar(
            required=False, description="Some additional Params for your offering"
        )
        policy = GenericScalar(
            required=False, description="Some additional Params for your offering"
        )

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        node=None,
        name=None,
        params=None,
        policy=None,
        extensions=[],
        version="main",
    ):
        registry, _ = Registry.objects.get_or_create(
            app=info.context.bounced.app, user=info.context.bounced.user
        )

        try:
            template = Template.objects.get(
                node=node, version=version, registry=registry
            )
            template.extensions = extensions
            template.params = params or {}
            template.save()

        except:
            template = Template.objects.create(
                node_id=node,
                params=params or {},
                registry=registry,
                extensions=extensions,
                version=version,
            )

        return template

    class Meta:
        type = types.Template
