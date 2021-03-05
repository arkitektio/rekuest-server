from balder.types import BalderQuery
from facade import types
from facade.models import DataModel, Node
import graphene
from herre import bounced


class ModelDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query node", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return DataModel.objects.get(id=id)

    class Meta:
        type = types.DataModel
        operation = "model"



class Models(BalderQuery):

    class Meta:
        type = types.DataModel
        list = True