from balder.types.mutation.base import BalderMutation
from lok import bounced
import graphene

from facade.enums import ReservationStatusInput
from facade.models import Reservation


class ResetReservationsReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetReservations(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(
            ReservationStatusInput, description="The status you want to get rid of"
        )
        pass

    class Meta:
        type = ResetReservationsReturn

    @bounced(anonymous=True)
    def mutate(root, info, exclude=[], name=None):

        for reservation in Reservation.objects.all():
            reservation.delete()

        return {"ok": True}
