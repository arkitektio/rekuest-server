from balder.types import BalderQuery
from facade import types, filters
from facade.models import Collection
import graphene
from lok import bounced
from itertools import chain


class CollectionDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query node", required=False)

    @bounced(anonymous=True)
    def resolve(root, info, id=None, identifier=None):
        if id:
            return Collection.objects.get(id=id)

    class Meta:
        type = types.Collection
        operation = "collection"


class Collections(BalderQuery):
    class Meta:
        type = types.Collection
        list = True
        filter= filters.CollectionFilter
        paginate = True
        operation = "collections"
