from facade.enums import ReservationStatus
from facade.helpers import create_context_from_bounced
from facade.models import Reservation, Waiter
from facade.subscriptions.waiter import WaiterSubscription
from facade import models, types
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnreserveMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class SlateMutation(BalderMutation):
    class Arguments:
        identifier = graphene.String(
            description="The identifier you want to slate", required=True
        )

    class Meta:
        type = graphene.List(
            graphene.ID, description="The ids of the reservation cancelled"
        )
        operation = "slate"

    @bounced(only_jwt=True)
    def mutate(root, info, identifier=""):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        registry, _ = models.Registry.objects.get_or_create(
            user=bounce.user, app=bounce.app
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=identifier
        )

        ids = []

        ress = Reservation.objects.filter(waiter=waiter)
        for res in ress:
            ids.append(res.id)
            res.delete()

        return ids
