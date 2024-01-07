from facade.models import Reservation
from facade import models
import uuid
from balder.types import BalderMutation
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
        str(uuid.uuid4())
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
