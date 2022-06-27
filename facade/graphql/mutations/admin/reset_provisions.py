from balder.types.mutation.base import BalderMutation

from lok import bounced
import graphene

from facade.inputs import ProvisionStatusInput
from facade.models import Provision


class ResetProvisionsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetProvisions(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(
            ProvisionStatusInput, description="The status you want to get rid of"
        )

    class Meta:
        type = ResetProvisionsReturn

    @bounced(anonymous=True)
    def mutate(root, info, exclude=[], name=None):

        for provision in Provision.objects.all():
            provision.delete()

        return {"ok": True}
