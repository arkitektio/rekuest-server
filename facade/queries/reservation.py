from facade.filters import NodeFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.models import Node, Reservation, ReservationStatus
import graphene
from herre import bounced


class ReservationDetailQuery(BalderQuery):

    class Arguments:
        reference = graphene.ID(description="The query reservation", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, reference=None):
        return Reservation.objects.get(reference=reference)

    class Meta:
        type = types.Reservation
        operation = "reservation"



class Reservations(BalderQuery):


    class Meta:
        type = types.Reservation
        list = True


class MyReservations(BalderQuery):

    @bounced(anonymous=False)
    def resolve(root, info, **kwargs):
        return Reservation.objects.filter(creator=info.context.user).exclude(status=ReservationStatus.ENDED.value).all()


    class Meta:
        type = types.Reservation
        personal = "creator"
        list = True