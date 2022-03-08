from django.db.models.signals import post_save
from django.dispatch.dispatcher import receiver
from facade.models import Assignation, Node, Provision, Reservation
from kombu import Queue


@receiver(post_save, sender=Node)
def samp_post_save(sender, instance=None, created=None, **kwargs):
    from facade.subscriptions import NodesEvent, NodeDetailEvent

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
    from facade.subscriptions import ReservationsSubscription

    if instance.waiter:
        print("Doin thig here")
        ReservationsSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"waiter_{instance.waiter.unique}"],
        )


@receiver(post_save, sender=Assignation)
def ass_post_save(sender, instance: Assignation = None, created=None, **kwargs):
    from facade.subscriptions import TodosSubscription

    if instance.waiter:
        print("Todos lets go")
        TodosSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"todos_{instance.waiter.unique}"],
        )

@receiver(post_save, sender=Provision)
def prov_post_save(sender, instance: Provision = None, created=None, **kwargs):
    from facade.subscriptions import MyProvisionsEvent

    if instance.creator:
        MyProvisionsEvent.broadcast(
            {"action": "created", "data": instance},
            [f"provisions_user_{instance.creator.id}"],
        )