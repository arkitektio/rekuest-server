from django.db.models.signals import post_save
from django.dispatch.dispatcher import receiver

from facade.models import Node


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
