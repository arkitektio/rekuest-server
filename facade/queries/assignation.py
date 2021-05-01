from facade.enums import AssignationStatus
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

    @bounced(anonymous=False)
    def resolve(root, info, **kwargs):
        return Assignation.objects.filter(creator=info.context.user).exclude(status=AssignationStatus.DONE.value).exclude(status=AssignationStatus.CANCELLED.value).all()

    class Meta:
        type = types.Assignation
        list = True