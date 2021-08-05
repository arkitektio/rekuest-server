from balder.types import BalderQuery
from facade import types
from facade.models import AppRepository, MirrorRepository, Repository, Node
import graphene
from herre import bounced
from itertools import chain

class RepoDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query node", required=False)

    @bounced(anonymous=True)
    def resolve(root, info, id=None, identifier=None):
        if id: return Repository.objects.get(id=id)

    class Meta:
        type = types.Repository
        operation = "repository"



class Repositories(BalderQuery):

    @bounced(anonymous=True)
    def resolve(root, info):
        print(MirrorRepository.objects.all())


        return chain(AppRepository.objects.all(), MirrorRepository.objects.all())

    class Meta:
        type = types.Repository
        list = True