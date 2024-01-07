import uuid
from facade import types
from facade import inputs
from facade.models import Provision, Reservation, Waiter, Registry, Node, Template
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
from hare.messages import ReserveParams, BindParams
from hare.connection import rmq
from django.contrib.auth import get_user_model
from facade.utils import get_imitiate

logger = logging.getLogger(__name__)  #


class ReserveMutation(BalderMutation):
    class Arguments:
        instance_id = graphene.ID(required=True)
        node = graphene.ID(required=False)
        template = graphene.ID(required=False)
        hash = graphene.String(required=False)
        reference = graphene.String(required=False)
        title = graphene.String(required=False)
        params = graphene.Argument(
            types.ReserveParamsInput, description="Additional Params", required=False
        )
        binds = graphene.Argument(
            inputs.ReserveBindsInput, description="bindings", required=False
        )
        persist = graphene.Boolean(
            default_value=True, description="Additional Params", required=False
        )
        allow_auto_request = graphene.Boolean(required=False)
        imitate = graphene.ID(required=False)
        provision = graphene.ID(required=False)

    class Meta:
        type = types.Reservation
        operation = "reserve"

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        node=None,
        hash=None,
        template=None,
        params={},
        binds=None,
        title=None,
        reference=None,
        persist=True,
        imitate=None,
        instance_id=None,
        provision=None,
        allow_auto_request=False,
    ):
        reference = reference
        params = ReserveParams(**params)

        if imitate:
            imitate = get_user_model().objects.get(id=imitate)
            assert info.context.user.has_perm(
                "imitate", imitate
            ), "You don't have permission to imitate this user"

        user = (
            info.context.user
            if imitate is None
            else get_imitiate(info.context.user, imitate)
        )
        client = info.context.bounced.client

        registry, _ = Registry.objects.update_or_create(
            user=user, client=client, defaults=dict(app=info.context.bounced.app)
        )
        waiter, _ = Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

        if provision:
            provision = Provision.objects.get(id=provision)

        assert (
            node or hash or template
        ), "Please provide either a node or template you want to reserve"

        if template:
            template = Template.objects.get(id=template)
            node = template.node.id
            binds = (
                BindParams(**binds)
                if binds
                else BindParams(templates=[template.id], clients=[])
            )
        else:
            binds = BindParams(**binds) if binds else None

        reference = reference or (binds.hash() if binds else "default")

        res, cr = Reservation.objects.update_or_create(
            node_id=node if node else Node.objects.get(hash=hash).id,
            reference=reference,
            waiter=waiter,
            defaults={
                "title": title,
                "params": params.dict(),
                "binds": binds.dict() if binds else None,
                "provision": provision,
                "allow_auto_request": allow_auto_request,
            },
        )
        return res
