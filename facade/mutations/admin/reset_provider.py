from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import HostProtocol, HostSettings, PointSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import Provider, Structure, DataPoint
from balder.enum import InputEnum
from facade.enums import ClientType, DataPointType, NodeType
from lok import bounced
import graphene
import logging
import namegenerator


class ResetProvidersReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetProviders(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        pass

    class Meta:
        type = ResetProvidersReturn 

    
    @bounced(anonymous=True) 
    def mutate(root, info, url=None,  name=None):

        for provider in Provider.objects.all():
            provider.active = False
            provider.save()

        return {"ok": True}