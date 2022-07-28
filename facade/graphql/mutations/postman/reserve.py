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
        app_group = graphene.ID(
            description="A unique identifier", required=False, default_value="default"
        )
        imitate = graphene.String(required=False)

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
        imitate=None,
        app_group=None,
    ):
        reference = reference or str(uuid.uuid4())
        params = ReserveParams(**params)

        if imitate:
            imitate = get_user_model().objects.get(id=imitate)
            assert info.context.user.has_perm(
                "imitate", imitate
            ), "You don't have permission to imitate this user"

        creator = info.context.bounced.user if not imitate else imitate
        app = info.context.bounced.app

        registry, _ = Registry.objects.get_or_create(user=creator, app=app)
        waiter, _ = Waiter.objects.get_or_create(
            registry=registry, identifier=app_group
        )

        assert (
            node or template
        ), "Please provide either a node or template you want to reserve"

        res, cr = Reservation.objects.get_or_create(
            node_id=node, params=params.dict(), waiter=waiter
        )
        res, forwards = res.schedule()

        for forward_res in forwards:
            rmq.publish(forward_res.queue, forward_res.to_message())

        return res
