from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import HostProtocol, HostSettings, PointSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import Provider, Provision, Reservation, Structure, DataPoint
from balder.enum import InputEnum
from facade.enums import ClientType, DataPointType, NodeType, ProvisionStatusInput
from lok import bounced
import graphene
import logging
import namegenerator


class ResetReservationsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetReservations(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(ProvisionStatusInput, description="The status you want to get rid of")
        pass

    class Meta:
        type = ResetReservationsReturn 

    
    @bounced(anonymous=True) 
    def mutate(root, info, exclude=[],  name=None):

        for reservation in Reservation.objects.all():
            reservation.delete()

        return {"ok": True}