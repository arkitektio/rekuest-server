from facade.enums import AssignationStatus
from facade.graphql.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation, Provision, Reservation
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.connection import rmq


logger = logging.getLogger(__name__)  #


class UnlinkMutation(BalderMutation):
    class Arguments:
        reservation = graphene.ID(required=True)
        provision = graphene.ID(required=True)
        safe = graphene.Boolean(required=False)

    class Meta:
        type = types.Provision
        operation = "unlink"

    @bounced(only_jwt=True)
    def mutate(root, info, reservation, provision, safe=False):

        res = Reservation.objects.get(id=reservation)
        prov = Provision.objects.get(id=provision)

        prov, forwards = prov.unlink(res)

        for forward_res in forwards:
            rmq.publish(forward_res.queue, forward_res.to_message())

        return prov
