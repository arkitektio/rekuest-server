from balder.types import BalderQuery
from facade import types
from facade.models import Structure, Node
import graphene
from lok import bounced


class StructureDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query node", required=False)
        identifier = graphene.String(description="The Identifier of this Model", required=False)

    @bounced(anonymous=True)
    def resolve(root, info, id=None, identifier=None):
        if id: return Structure.objects.get(id=id)
        if identifier: return Structure.objects.get(identifier=identifier)

    class Meta:
        type = types.Structure
        operation = "structure"



class Structures(BalderQuery):

    class Meta:
        type = types.Structure
        list = True