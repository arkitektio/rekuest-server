from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from facade import models, channels, channel_events, transport

import logging


logger = logging.getLogger(__name__)
logger.info("Loading sssignals")

_UNSET = object()


def _broadcast_on_commit(channel, event, topics=_UNSET):
    """Fan an event out only once the surrounding transaction commits.

    Signals fire while the writing transaction is still open (e.g. the atomic
    ``implement_agent`` reconcile). Broadcasting immediately would publish events for rows
    that may still roll back, and serializes the channel-layer work inside the lock
    window. Outside a transaction ``on_commit`` runs the callback immediately, so
    non-transactional paths are unaffected.
    """
    if topics is _UNSET:
        transaction.on_commit(lambda: channel.broadcast(event))
    else:
        transaction.on_commit(lambda: channel.broadcast(event, topics))


@receiver(post_save, sender=models.State)
def state_post_save(sender, instance: models.State = None, created=None, **kwargs):
    _broadcast_on_commit(channels.state_update_channel, channel_events.StateUpdateEvent(state=instance.id), [f"state_{instance.id}"])


@receiver(post_save, sender=models.Action)
def action_singal(sender, instance=None, created=None, **kwargs):
    if instance:
        if created:
            _broadcast_on_commit(channels.action_channel, channel_events.ActionEvent(create=instance.id), [f"actions_{instance.organization.id}"])
        else:
            _broadcast_on_commit(channels.action_channel, channel_events.ActionEvent(update=instance.id), [f"actions_{instance.organization.id}"])


def broadcast_agent_update(agent: models.Agent, created: bool = False) -> None:
    """Refresh the agent feeds — callable directly when a change needs no row write (an M2M
    edit), so nobody has to ``save()`` an agent merely to fire this signal."""
    _broadcast_on_commit(
        channels.agent_updated_channel,
        channel_events.AgentEvent(create=agent.id) if created else channel_events.AgentEvent(update=agent.id),
        [f"agents_for_{agent.organization_id}"],
    )


@receiver(post_save, sender=models.Agent)
def agent_post_save(sender, instance: models.Agent = None, created=None, **kwargs):
    if instance:
        broadcast_agent_update(instance, created=bool(created))
        # The writing backend sees its own routing change at once (other backends within the
        # cache's few-second TTL) — see ``facade.transport``.
        transport.forget_agent_routing(instance)


@receiver(post_delete, sender=models.Agent)
def agent_post_delete(sender, instance: models.Agent = None, **kwargs):
    if instance:
        _broadcast_on_commit(
            channels.agent_updated_channel,
            channel_events.AgentEvent(delete=instance.id),
            [f"agents_for_{instance.organization.id}"],
        )


@receiver(post_save, sender=models.Task)
def task_post_save(sender, instance: models.Task = None, created=None, **kwargs):
    # Root-task change feed: a freshly created root task is fanned out to both the caller's
    # feed (mytasks) and the org-wide feed (tasks). Child tasks never reach these feeds.
    if created and instance.root_id is None and instance.caller_id:
        _broadcast_on_commit(
            channels.task_event_channel,
            channel_events.TaskEventCreatedEvent(create=channel_events.TaskChangePayload.from_task(instance)),
            [
                f"root_tasks_caller_{instance.caller_id}",
                f"root_tasks_org_{instance.caller.organization_id}",
            ],
        )

    # Agent feed: any task (root or child) run by an agent is fanned out to that agent's
    # detail-page feed, so the agent's "latest tasks" list updates live on create and on
    # every status/is_done transition (which re-saves the Task row → arrives here as update).
    if instance.agent_id:
        payload = channel_events.TaskChangePayload.from_task(instance)
        event = channel_events.ChildTaskEvent(create=payload) if created else channel_events.ChildTaskEvent(update=payload)
        _broadcast_on_commit(channels.agent_task_channel, event, [f"agent_tasks_{instance.agent_id}"])

    # Detail feed: notify the direct parent AND the root, so a subscription on the root task
    # sees the whole subtree while an intermediate task still sees its direct children.
    if instance.parent_id:
        topics = {f"child_tasks_{instance.parent_id}"}
        if instance.root_id:
            topics.add(f"child_tasks_{instance.root_id}")
        payload = channel_events.TaskChangePayload.from_task(instance)
        event = channel_events.ChildTaskEvent(create=payload) if created else channel_events.ChildTaskEvent(update=payload)
        _broadcast_on_commit(channels.child_task_channel, event, list(topics))


@receiver(post_save, sender=models.TaskEvent)
def task_event_post_save(sender, instance: models.TaskEvent = None, created=None, **kwargs):
    logger.info("Task Event received")
    # One typed publisher fans the persisted event out to its caller (channel layer for the
    # GraphQL subscription + live WS forward, and a webhook POST for a HookAgent caller).
    transaction.on_commit(lambda instance=instance: transport.publish_task_event(instance))


@receiver(post_save, sender=models.Implementation)
def implementation_post_save(sender, instance: models.Implementation = None, created=None, **kwargs):
    # Two audiences: the per-implementation detail feed (implementation_change) and the
    # per-agent list feed (implementations) — every event reaches both.
    topics = [f"implementation_{instance.id}", f"implementations_agent_{instance.agent_id}"]
    event = channel_events.ImplementationEvent(create=instance.id) if created else channel_events.ImplementationEvent(update=instance.id)
    _broadcast_on_commit(channels.new_implementation_channel, event, topics)


@receiver(post_delete, sender=models.Implementation)
def implementation_post_del(sender, instance: models.Implementation = None, **kwargs):
    if instance:
        _broadcast_on_commit(
            channels.new_implementation_channel,
            channel_events.ImplementationEvent(delete=instance.id),
            [f"implementation_{instance.id}", f"implementations_agent_{instance.agent_id}"],
        )


@receiver(post_save, sender=models.Patch)
def patch_post_save(sender, instance: models.Patch = None, created=None, **kwargs):
    if created:
        topics = [f"patches_state_{instance.state_id}"]
        if instance.agent_id:
            topics.append(f"patches_agent_{instance.agent_id}")

        _broadcast_on_commit(channels.patch_channel, channel_events.PatchEvent.from_patch(instance), topics)
