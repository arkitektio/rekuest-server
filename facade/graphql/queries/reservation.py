from balder.types import BalderQuery
from facade import types, models
from facade.models import Reservation
import graphene
from lok import bounced

from facade.inputs import ReservationStatusInput


class ReservationDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query reservation", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Reservation.objects.get(id=id)

    class Meta:
        type = types.Reservation
        operation = "reservation"


class Reservations(BalderQuery):
    class Meta:
        type = types.Reservation
        list = True


class MyReservations(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            ReservationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ReservationStatusInput, description="The included values", required=False
        )

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None):
        qs = Reservation.objects.filter(waiter__registry__user=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Reservation
        list = True
        paginate = True


class WaitListQuery(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            ReservationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ReservationStatusInput, description="The included values", required=False
        )
        app_group = graphene.ID(required=False, default_value="main")

    @bounced(only_jwt=True)
    def resolve(root, info, exclude=None, filter=None, app_group="main"):

        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = models.Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=app_group
        )

        qs = Reservation.objects.filter(waiter=waiter)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Reservation
        list = True
        operation = "waitlist"
