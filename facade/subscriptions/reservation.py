from facade.enums import ReservationStatus
from graphene.types.scalars import String
from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class ReservationLogEvent(graphene.ObjectType):
    message = graphene.String()
    level = graphene.String()


class ReservationEvent(graphene.ObjectType):
    log = graphene.Field(ReservationLogEvent)


class ReservationEventSubscription(BalderSubscription):

    class Arguments:
        reference = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, reference=None, level=None):
        reservation = models.Reservation.objects.get(reference=reference)
        assert reservation.creator == info.context.bounced.user, "You cannot listen to a reservation that you have not created"
        return [f"reservation_{reservation.reference}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        print(payload)

        if action == "log":
            return {"log": data}

        if action == "update":
            return {"log": models.Reservation.objects.get(id=data)}

        print("error in payload")


    class Meta:
        type = ReservationEvent
        operation = "reservationEvent"


class ReservationsEvent(graphene.ObjectType):
    ended =  graphene.ID()
    update =  graphene.Field(types.Reservation)
    create = graphene.Field(types.Reservation)



class MyReservationsEvent(BalderSubscription):


    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        print(f"reservations_user_{info.context.user.id}")
        return [f"reservations_user_{info.context.user.id}"]


    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        print(payload)

        if action == "created":
            return {"create": models.Reservation.objects.get(id=data)}
        if action in [ReservationStatus.CANCELLED, ReservationStatus.ENDED]:
            return {"ended": data}
        else:
            return {"update": models.Reservation.objects.get(id=data)}



    class Meta:
        type = ReservationsEvent
        operation = "myReservationsEvent"



