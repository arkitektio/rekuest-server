from typing import List
from facade.scalars import Callback, CallbackContainer
from facade.helpers import create_context_from_bounced
from facade.subscriptions.reservation import MyReservationsEvent
from facade.subscriptions.waiter import WaiterSubscription
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from facade import types
from facade.enums import ReservationStatus
from facade.models import Reservation, Waiter, Registry
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

from django.contrib.auth import get_user_model

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
        callbacks = graphene.List(Callback, required=False)
        app_group = graphene.ID(
            description="A unique identifier", required=False, default_value="main"
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
        callbacks: List[CallbackContainer] = [],
    ):
        reference = reference or str(uuid.uuid4())

        creator = info.context.bounced.user
        app = info.context.bounced.app

        print(app_group)
        registry, _ = Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = Waiter.objects.get_or_create(
            registry=registry, identifier=app_group
        )
        print(waiter)

        for callback in callbacks:
            print(callback)

        assert (
            node or template
        ), "Please provide either a node or template you want to reserve"

        res, created = Reservation.objects.get_or_create(
            node_id=node,
            template_id=template,
            creator=creator,
            params=params,
            app=app,
            waiter=waiter,
            defaults={
                "status": ReservationStatus.ROUTING,
                "title": title,
                "extensions": {"persist": persist},
                "reference": reference,
                "callback": "not-set",
                "progress": "not-set",
                "params": params,
            },
        )

        if creator:
            MyReservationsEvent.broadcast(
                {"action": "created" if created else "update", "data": res.id},
                [f"reservations_user_{creator.id}"],
            )

        """  #bounced = BouncedReserveMessage(
        #    data={"node": node, "template": template, "params": params},
         #   meta={
                "reference": reference,
                "extensions": {
                    "callback": "not-set",
                    "progress": "not-set",
                },
                "context": create_context_from_bounced(bounce),
            },
        )

        GatewayConsumer.send(bounced)
        """
        return res
