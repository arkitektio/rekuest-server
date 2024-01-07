from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch.dispatcher import receiver
from facade.models import (
    Agent,
    Assignation,
    AssignationLog,
    Node,
    Provision,
    Reservation,
    Template,
)
import logging
from guardian.shortcuts import assign_perm
from django.contrib.auth import get_user_model
from hare.connection import pikaconnection
from hare.carrots import (
    HareMessage,
    ProvideHareMessage,
    ReserveHareMessage,
    UnprovideHareMessage,
    UnreserveHareMessage,
    ReservationChangedMessage,
)

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Node)
def node_post_save(sender, instance=None, created=None, **kwargs):
    from facade.graphql.subscriptions import NodesEvent, NodeDetailEvent

    NodesEvent.broadcast(
        {"action": "created", "data": instance}
        if created
        else {"action": "updated", "data": instance},
        ["all_nodes"],
    )

    NodeDetailEvent.broadcast(
        {"action": "updated", "data": instance}, [f"node_{instance.id}"]
    )

    if instance.interfaces:
        for interface in instance.interfaces:
            NodesEvent.broadcast(
                {"action": "created", "data": instance}
                if created
                else {"action": "updated", "data": instance},
                [f"interface_{interface}"],
            )


@receiver(post_save, sender=Template)
def template_post_save(sender, instance=None, created=None, **kwargs):
    from facade.graphql.subscriptions import TemplatesEvent

    if instance.params:
        TemplatesEvent.broadcast(
            {"action": "created", "data": instance}
            if created
            else {"action": "updated", "data": instance},
            [
                TemplatesEvent.PARAMS_GROUP(key, value)
                for key, value in instance.params.items()
            ],
        )


@receiver(pre_delete, sender=Provision)
def prov_pre_delete(sender, instance=None, created=None, **kwargs):
    """Unreserve this reservation"""
    from facade.graphql.subscriptions import (
        ReservationsSubscription,
        MyReservationsSubscription,
    )

    forwards = []

    for reservation in instance.caused_reservations.all():
        reservation.delete()

    if instance.agent:
        forwards.append(
            UnprovideHareMessage(queue=instance.agent.queue, provision=instance.id)
        )

    for forward_res in forwards:
        pikaconnection.publish(forward_res.queue, forward_res.to_message())


@receiver(post_delete, sender=Node)
def node_post_del(sender, instance=None, created=None, **kwargs):
    from facade.graphql.subscriptions import NodesEvent, NodeDetailEvent

    print

    if instance.interfaces:
        for interface in instance.interfaces:
            NodesEvent.broadcast(
                {"action": "delete", "data": instance.id},
                [f"interface_{interface}"],
            )


@receiver(post_save, sender=Template)
def template_post_save(sender, instance=None, created=None, **kwargs):
    if created:
        print("CREATED TEMPLATE", instance)
        assign_perm("providable", instance.agent.registry.user, instance)


@receiver(post_save, sender=AssignationLog)
def ass_log_post_save(sender, instance: AssignationLog = None, created=None, **kwargs):
    from facade.graphql.subscriptions import AssignationSubscription

    logging.error("asdasd")
    if instance.assignation:
        AssignationSubscription.broadcast(
            {"action": "log", "data": instance},
            [f"assignation_{instance.assignation.id}"],
        )


@receiver(post_save, sender=Assignation)
def ass_post_save(sender, instance: Assignation = None, created=None, **kwargs):
    from facade.graphql.subscriptions import (
        RequestsSubscription,
        MyRequestsSubscription,
    )

    if instance.reservation:
        RequestsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [
                f"requests_{instance.reservation.waiter.unique}",
            ],
        )
        MyRequestsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [
                f"myrequests_{instance.reservation.waiter.registry.user.id}",
            ],
        )


@receiver(post_save, sender=Provision)
def prov_post_save(sender, instance: Provision = None, created=None, **kwargs):
    from facade.graphql.subscriptions import ProvisionSubscription
    from facade.enums import ProvisionStatus

    if created:
        assign_perm("can_link_to", instance.creator, instance)

    if instance.status == ProvisionStatus.ACTIVE:
        logging.info(
            f"Provision {instance}: is now active. Broadcasting this state to all reservations..."
        )
        # Check if any reservation is using this provision and see if it needs to be set to active

    if instance.status == ProvisionStatus.CANCELLED:
        logging.info(
            f"Provision {instance}: is now cancelled. Broadcasting this state to all reservations..."
        )

    if instance.status == ProvisionStatus.CANCELING:
        logging.info(
            f"Provision {instance}: was requsested to be cancselsled. Sending tshiss to tssshe agent.."
        )

    if instance.id:
        ProvisionSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"provision_{instance.id}"],
        )


@receiver(post_save, sender=Reservation)
def res_post_save(sender, instance: Reservation = None, created=None, **kwargs):
    from facade.graphql.subscriptions import (
        ReservationsSubscription,
        MyReservationsSubscription,
    )

    if created:
        res, forwards = instance.schedule()

        for forward_res in forwards:
            pikaconnection.publish(forward_res.queue, forward_res.to_message())

    else:
        ReservationsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [
                f"reservations_{instance.waiter.unique}",
            ],
        )
        MyReservationsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [
                f"myreservations_{instance.waiter.registry.user.id}",
            ],
        )

        print("UPDATEDING RESERVATION", instance)

        if instance.provision:
            ReservationsSubscription.broadcast(
                {"action": "create" if created else "update", "data": instance},
                [
                    f"reservations_provision_{instance.provision.id}",
                ],
            )


@receiver(post_save, sender=get_user_model())
def user_saved(sender, instance: Reservation = None, created=None, **kwargs):
    from django.conf import settings
    from django.contrib.auth.models import Group

    logger.info(f"User {instance} was saved")

    if created:
        for group_name in settings.IMITATE_GROUPS:
            g, _ = Group.objects.get_or_create(name=group_name)
            assign_perm(
                "lok.imitate",
                g,
            )


@receiver(post_save, sender=Agent)
def agen_post_save(sender, instance: Agent = None, created=None, **kwargs):
    from facade.graphql.subscriptions import AgentsEvent

    print("UPDATEDING AGENT", instance)

    if instance.registry.user:
        print("SENDING TO USER", instance.registry.user.id)
        AgentsEvent.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"agents_user_{instance.registry.user.id}"],
        )
