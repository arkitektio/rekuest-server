from balder.types import BalderQuery
from facade import types, models
import graphene
from herre import bounced


class AccessorDetailQuery(BalderQuery):

    class Arguments:
        model = graphene.ID(description="The query node", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, model=None):
        return models.Accessor.objects.get(model_id=model)

    class Meta:
        type = types.Accessor
        operation = "accesor"



class Accessor(BalderQuery):

    class Meta:
        type = types.Accessor
        list = True