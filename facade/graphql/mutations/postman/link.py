from facade.enums import AssignationStatus
from facade.graphql.subscriptions.assignation import MyAssignationsEvent
from facade.models import Assignation, Provision, Reservation
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging


logger = logging.getLogger(__name__)  #


class LinkMutation(BalderMutation):
    class Arguments:
        reservation = graphene.ID(required=True)
        provision = graphene.ID(required=True)

    class Meta:
        type = types.Provision
        operation = "link"

    @bounced(only_jwt=True)
    def mutate(root, info, reservation, provision):

        res = Reservation.objects.get(id=reservation)
        prov = Provision.objects.get(id=provision)

        assert info.context.user.has_perms(
            ["facade.can_link_to"], prov
        ), "You don't have permission to link to this provision"

        prov.reservations.add(res)
        prov.save()

        return prov
