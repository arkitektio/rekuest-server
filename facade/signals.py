from django.db.models.signals import post_save
from django.dispatch.dispatcher import receiver
from facade.models import (
    Agent,
    Assignation,
    AssignationLog,
    Node,
    Provision,
    Reservation,
)
import logging


@receiver(post_save, sender=Node)
def samp_post_save(sender, instance=None, created=None, **kwargs):
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


@receiver(post_save, sender=Reservation)
def res_post_save(sender, instance=None, created=None, **kwargs):
    from facade.graphql.subscriptions import ReservationsSubscription

    logging.error("asdasd")
    if instance.waiter:
        logging.error("CALLLLED")
        ReservationsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"reservations_waiter_{instance.waiter.unique}"],
        )


@receiver(post_save, sender=AssignationLog)
def ass_log_post_save(sender, instance=None, created=None, **kwargs):
    from facade.graphql.subscriptions import AssignationEventSubscription

    logging.error("asdasd")
    if instance.assignation:
        logging.error("CALLLLED")
        AssignationEventSubscription.broadcast(
            {"action": "log", "data": instance},
            [f"assignation_{instance.assignation.id}"],
        )


@receiver(post_save, sender=Assignation)
def ass_post_save(sender, instance: Assignation = None, created=None, **kwargs):
    from facade.graphql.subscriptions import TodosSubscription, MyAssignationsEvent

    if instance.reservation.waiter:
        MyAssignationsEvent.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"assignations_user_{instance.reservation.waiter.registry.user.id}"],
        )


@receiver(post_save, sender=Provision)
def prov_post_save(sender, instance: Provision = None, created=None, **kwargs):
    from facade.graphql.subscriptions import MyProvisionsEvent

    if instance.reservation:
        MyProvisionsEvent.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"provisions_waiter_{instance.reservation.waiter.unique}"],
        )


@receiver(post_save, sender=Agent)
def agen_post_save(sender, instance: Agent = None, created=None, **kwargs):
    from facade.graphql.subscriptions import AgentsEvent

    if instance.registry.user:
        AgentsEvent.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"agents_user_{instance.registry.user.id}"],
        )

    AgentsEvent.broadcast(
        {"action": "create" if created else "update", "data": instance},
        [f"all_agents"],
    )
