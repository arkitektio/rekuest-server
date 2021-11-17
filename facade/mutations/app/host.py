from facade import models
from facade.models import Registry, Repository, Structure, Template
from facade import types
from balder.types import BalderMutation
import graphene
from lok import bounced
from graphene.types.generic import GenericScalar


class Host(BalderMutation):
    class Arguments:
        identifier = graphene.String(
            required=True, description="The Model you are trying to host"
        )
        extenders = graphene.List(
            graphene.String,
            required=False,
            description="Some additional Params for your offering",
        )

    @bounced(only_jwt=True, required_scopes=["provider"])
    def mutate(root, info, identifier=None, extenders=[]):
        repository = Repository.objects.get(
            app=info.context.bounced.app, user=info.context.bounced.user
        )

        try:
            model = Structure.objects.get(repository=repository, identifier=identifier)
        except:
            model = Structure.objects.create(
                identifier=identifier, extenders=extenders, repository=repository
            )
        return model

    class Meta:
        type = types.Structure
