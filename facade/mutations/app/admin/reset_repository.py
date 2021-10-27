from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import HostProtocol, HostSettings, PointSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import AppRepository, Node, Structure, DataPoint
from balder.enum import InputEnum
from facade.enums import ClientType, DataPointType, NodeType
from lok import bounced
import graphene
import logging
import namegenerator


class ResetRepositoryReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetRepository(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        pass

    class Meta:
        type = ResetRepositoryReturn 

    
    @bounced(anonymous=False) 
    def mutate(root, info, url=None,  name=None):
        repository = AppRepository.objects.get(app=info.context.bounced.app)

        for node in Node.objects.filter(repository=repository).all():
            node.delete()

        return {"ok": True}