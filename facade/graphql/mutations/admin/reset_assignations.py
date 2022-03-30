from balder.types.mutation.base import BalderMutation
from lok import bounced
import graphene

from facade.structures.inputs import AssignationStatusInput
from facade.models import Assignation


class ResetAssignationsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetAssignations(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(
            AssignationStatusInput, description="The status you want to get rid of"
        )

    class Meta:
        type = ResetAssignationsReturn

    @bounced(anonymous=True)
    def mutate(root, info, exclude=[], name=None):

        for reservation in Assignation.objects.all():
            reservation.delete()

        return {"ok": True}
