from facade.enums import ReservationStatusInput
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

    class Arguments:
        exclude = graphene.List(ReservationStatusInput, description="The excluded values", required=False)
        filter = graphene.List(ReservationStatusInput, description="The included values", required=False)


    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None):
        qs = Reservation.objects.filter(creator=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Reservation
        list = True