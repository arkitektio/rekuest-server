import uuid
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from facade import models, types
from hare.connection import rmq

logger = logging.getLogger(__name__)  #


class UnprovideReturn(graphene.ObjectType):
    id = graphene.ID()


class UnprovideMutation(BalderMutation):
    class Arguments:
        id = graphene.ID(
            description="The reference of the Provision you want to ruin"
        )

    class Meta:
        type = UnprovideReturn
        operation = "unprovide"

    @bounced(only_jwt=True)
    def mutate(root, info, id=None):

        provision = models.Provision.objects.get(id=id)
        provision.delete()


        return {"id": id}
