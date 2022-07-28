import uuid
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from facade import models, types
from facade.enums import ProvisionStatus
from hare.connection import rmq

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
        prov, forwards = provision.unprovide()

        for forward_res in forwards:
            rmq.publish(forward_res.queue, forward_res.to_message())

        return provision
