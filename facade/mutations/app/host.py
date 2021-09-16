from facade import models
from facade.models import Structure, DataPoint, Template
from facade import types
from balder.types import BalderMutation
import graphene
from lok import bounced
from graphene.types.generic import GenericScalar



class Host(BalderMutation):

    class Arguments:
        identifier = graphene.String(required=True, description="The Model you are trying to host")
        extenders  = graphene.List(graphene.String, required=False, description="Some additional Params for your offering")


    @bounced(only_jwt=True, required_scopes=["provider"])
    def mutate(root, info, identifier=None, extenders=[]):
        point = DataPoint.objects.get(app=info.context.bounced.app, user=info.context.bounced.user)

        try:
            model = Structure.objects.get(point=point, identifier=identifier)
        except:
            model = Structure.objects.create(
                identifier=identifier,
                extenders=extenders,
                point=point
            )
        # We check ids because AppProvider is not Provider subclass
        assert model.point.id == point.id, "Template cannot be offered because it already existed on another Provider, considering Implementing it differently or copy that implementation!"
        model.save()
        return model
        


    class Meta:
        type = types.Structure