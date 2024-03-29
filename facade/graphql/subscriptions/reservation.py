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


class ReservationSubscription(BalderSubscription):
    class Arguments:
        id = graphene.ID(description="The reference of the assignation", required=True)
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, id, level=None):
        reservation = models.Reservation.objects.get(id=id)
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

    class Meta:
        type = ReservationEvent
        operation = "reservation"


class MyReservationsSubscription(BalderSubscription):
    class Arguments:
        level = graphene.String(description="The log level for alterations")

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, **kwargs):
        return [f"myreservations_{info.context.user.id}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "created":
            return {"create": models.Reservation.objects.get(id=data)}
        if action in [ReservationStatus.CANCELLED, ReservationStatus.ENDED]:
            return {"ended": data}
        else:
            return {"update": models.Reservation.objects.get(id=data)}

    class Meta:
        type = ReservationsEvent
        operation = "myreservations"


class ReservationsSubscription(BalderSubscription):
    class Arguments:
        instance_id = graphene.String(
            description="The reference of this waiter", required=True
        )
        provision = graphene.String(
            description="The reference of the provision (if we want to only listen to this)",
            required=False,
        )

    @bounced(only_jwt=True)
    def subscribe(root, info, *args, instance_id=None, provision=None):
        client = info.context.bounced.client
        user = info.context.bounced.user

        registry, _ = models.Registry.objects.update_or_create(
            user=user, client=client, defaults=dict(app=info.context.bounced.app)
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

        if provision:
            provision = models.Provision.objects.get(id=provision)
            return [f"reservations_provision_{provision.id}"]

        return [f"reservations_{waiter.unique}"]

    def publish(payload, info, *args, **kwargs):
        payload = payload["payload"]
        action = payload["action"]
        data = payload["data"]

        if action == "delete":
            return {"delete": data}

        if action == "update":
            return {"update": data}

        if action == "create":
            return {"create": data}

    class Meta:
        type = ReservationsEvent
        operation = "reservations"
