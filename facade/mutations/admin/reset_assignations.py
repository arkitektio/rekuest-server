from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import HostProtocol, HostSettings, PointSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import Assignation, Provider, Provision, Reservation, Structure, DataPoint
from balder.enum import InputEnum
from facade.enums import AssignationStatusInput, ClientType, DataPointType, NodeType, ProvisionStatusInput
from lok import bounced
import graphene
import logging
import namegenerator


class ResetAssignationsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetAssignations(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(AssignationStatusInput, description="The status you want to get rid of")
        pass

    class Meta:
        type = ResetAssignationsReturn 

    
    @bounced(anonymous=True) 
    def mutate(root, info, exclude=[],  name=None):

        for reservation in Assignation.objects.all():
            reservation.delete()

        return {"ok": True}