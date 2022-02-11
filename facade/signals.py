from django.db.models.signals import post_save
from django.dispatch.dispatcher import receiver

from facade.models import Assignation, Node, Reservation


@receiver(post_save, sender=Node)
def samp_post_save(sender, instance=None, created=None, **kwargs):
    from facade.subscriptions import NodesEvent, NodeDetailEvent

    NodesEvent.broadcast(
        {"action": "created", "data": instance.id}
        if created
        else {"action": "updated", "data": instance.id},
        ["all_nodes"],
    )

    NodeDetailEvent.broadcast(
        {"action": "updated", "data": instance.id}, [f"node_{instance.id}"]
    )


@receiver(post_save, sender=Reservation)
def res_post_save(sender, instance=None, created=None, **kwargs):
    from facade.subscriptions import WaiterSubscription

    if instance.waiter:
        print("Doin thig here")
        WaiterSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"waiter_{instance.waiter.unique}"],
        )


@receiver(post_save, sender=Assignation)
def ass_post_save(sender, instance=None, created=None, **kwargs):
    from facade.subscriptions import TodosSubscription

    if instance.waiter:
        print("Doin thig here")
        TodosSubscription.broadcast(
            {"action": "create" if created else "update", "data": instance},
            [f"todos_{instance.waiter.unique}"],
        )
