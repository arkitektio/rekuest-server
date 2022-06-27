from typing import List
import uuid
from facade import types
from facade.enums import ReservationStatus
from facade.models import Reservation, Waiter, Registry
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.messages import ReserveParams
from hare.connection import rmq

logger = logging.getLogger(__name__)  #


class ReserveMutation(BalderMutation):
    class Arguments:
        node = graphene.ID(required=False)
        template = graphene.ID(required=False)
        reference = graphene.String(required=False)
        title = graphene.String(required=False)
        params = graphene.Argument(
            types.ReserveParamsInput, description="Additional Params", required=False
        )
        persist = graphene.Boolean(
            default_value=True, description="Additional Params", required=False
        )
        app_group = graphene.ID(
            description="A unique identifier", required=False, default_value="default"
        )
        creator = graphene.ID(required=False)

    class Meta:
        type = types.Reservation
        operation = "reserve"

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        node=None,
        template=None,
        params={},
        title=None,
        reference=None,
        persist=True,
        creator=None,
        app_group=None,
    ):
        reference = reference or str(uuid.uuid4())
        params = ReserveParams(**params)

        creator = info.context.bounced.user
        app = info.context.bounced.app

        registry, _ = Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = Waiter.objects.get_or_create(
            registry=registry, identifier=app_group
        )

        assert (
            node or template
        ), "Please provide either a node or template you want to reserve"

        try:
            res = Reservation.objects.get(
                node_id=node, params=params.dict(), waiter=waiter
            )
            res.statusmessage = "Wait for your reservation to come alive"

            if (
                res.status == ReservationStatus.CANCELLED
                or res.status == ReservationStatus.CANCELING
                or res.status == ReservationStatus.REROUTING
            ):
                res.statusmessage = (
                    "This reservation was cancelled and we need to reschedule it."
                )

                res, forward = Reservation.objects.reschedule(id=res.id)

        except Reservation.DoesNotExist:
            res, forward = Reservation.objects.schedule(
                node=node, params=params, waiter=waiter, title=title
            )

            for forward_res in forward:
                rmq.publish(forward_res.queue, forward_res.to_message())

        return res
