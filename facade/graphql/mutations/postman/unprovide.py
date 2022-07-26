import uuid
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from facade import models, types
from facade.enums import ProvisionStatus

logger = logging.getLogger(__name__)  #


class Unprovide(graphene.ObjectType):
    reference = graphene.ID()


class UnprovideMutation(BalderMutation):
    class Arguments:
        provision = graphene.ID(
            description="The reference of the Provision you want to ruin"
        )

    class Meta:
        type = types.Provision
        operation = "unprovide"

    @bounced(only_jwt=True)
    def mutate(root, info, provision=None):

        provision = models.Provision.objects.get(id=provision)
        provision.status == ProvisionStatus.CANCELING
        provision.save()

        return provision
