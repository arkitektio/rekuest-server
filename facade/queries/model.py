from balder.types import BalderQuery
from facade import types
from facade.models import DataModel, Node
import graphene
from herre import bounced


class ModelDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query node", required=False)
        identifier = graphene.String(description="The Identifier of this Model", required=False)

    @bounced(anonymous=True)
    def resolve(root, info, id=None, identifier=None):
        if id: return DataModel.objects.get(id=id)
        if identifier: return DataModel.objects.get(identifier=identifier)

    class Meta:
        type = types.DataModel
        operation = "model"



class Models(BalderQuery):

    class Meta:
        type = types.DataModel
        list = True