from facade.enums import ReservationStatus
from lok import bounced
from balder.types import BalderSubscription
from facade import models, types
import graphene


class ReservationLogEvent(graphene.ObjectType):
    message = graphene.String()
    level = graphene.String()


class ReservationsEvent(graphene.ObjectType):
    update = graphene.Field(types.Reservation)
    delete = graphene.Field(graphene.ID)
    create = graphene.Field(types.Reservation)


class ReservationEvent(graphene.ObjectType):
    log = graphene.Field(ReservationLogEvent)


class ReservationEventSubscription(BalderSubscription):
    class Arguments:
        reference = graphene.ID(
            description="The reference of the assignation", required=True
        )
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, reference=None, level=None):
        reservation = models.Reservation.objects.get(reference=reference)
        assert (
            reservation.creator == info.context.bounced.user
        ), "You cannot listen to a reservation that you have not created"
        return [f"reservation_{reservation.reference}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "log":
            return {"log": data}

        if action == "update":
            return {"log": models.Reservation.objects.get(id=data)}

        print("error in payload")

    class Meta:
        type = ReservationEvent
        operation = "reservationEvent"


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


class ReservationsSubscription(BalderSubscription):
    class Arguments:
        identifier = graphene.ID(
            description="The reference of this waiter", required=True
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, identifier=None):
        registry, _ = models.Registry.objects.get_or_create(
            user=info.context.bounced.user, app=info.context.bounced.app
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=identifier
        )
        print(f"Connected Waiter for {waiter}")
        return [f"reservations_waiter_{waiter.unique}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]
        print("received Payload", payload)

        if action == "delete":
            return {"delete": data}

        if action == "update":
            return {"update": data}

        if action == "create":
            return {"create": data}

        print("error in payload")

    class Meta:
        type = ReservationsEvent
        operation = "reservations"
