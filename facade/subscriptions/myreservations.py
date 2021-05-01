from herre.bouncer.utils import bounced
from balder.types import BalderSubscription
from facade.types import Reservation
from facade import models
import graphene


class ReservationsEvent(graphene.ObjectType):
    ended =  graphene.ID()
    update =  graphene.Field(Reservation)
    create = graphene.Field(Reservation)



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

        if action == "updated":
            return {"update": models.Reservation.objects.get(id=data)}
        if action == "created":
            return {"create": models.Reservation.objects.get(id=data)}
        if action == "ended":
            return {"ended": data}

        print("error in payload")


    class Meta:
        type = ReservationsEvent
        operation = "myReservationsEvent"


