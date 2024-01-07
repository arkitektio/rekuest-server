from facade.models import Provision, Reservation
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.connection import pikaconnection


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
            pikaconnection.publish(forward_res.queue, forward_res.to_message())

        return prov
