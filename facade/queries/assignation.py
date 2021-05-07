from facade.enums import AssignationStatus, AssignationStatusInput
from facade.filters import NodeFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.models import Assignation, Node, Reservation, ReservationStatus
import graphene
from herre import bounced

class AssignationDetailQuery(BalderQuery):

    class Arguments:
        reference = graphene.ID(description="The query assignation", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, reference=None):
        return Assignation.objects.get(reference=reference)

    class Meta:
        type = types.Assignation
        operation = "assignation"


class MyAssignations(BalderQuery):

    class Arguments:
        exclude = graphene.List(AssignationStatusInput, description="The excluded values", required=False)
        filter = graphene.List(AssignationStatusInput, description="The included values", required=False)


    @bounced(anonymous=False)
    def resolve(root, info, exclude=[], filter=None):
        qs = Assignation.objects.filter(creator=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        for value in exclude:
            qs = qs.exclude(status=value)

        return qs.all()

    class Meta:
        type = types.Assignation
        list = True