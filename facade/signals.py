from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from authentikate.models import Organization
from facade import models, channels, channel_events

import logging


logger = logging.getLogger(__name__)

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


@receiver(post_save, sender=Organization)
def organization_post_save(sender, instance: Organization = None, created=None, **kwargs):
    """A new organization gets every hook agent on the fly (an organization takt was
    first to see has no such moment; the provisioning pass gives it its agents)."""
    if created:
        from facade import hook_agents

        transaction.on_commit(lambda: hook_agents.provision_new_organization(instance))


def _broadcast_rule(which: str, instance, change: str) -> None:
    """Tell the organization's rule feed. Bookkeeping takt writes with its own SQL is published by takt."""
    organization = models.Caller.objects.filter(pk=instance.caller_id).values_list("organization_id", flat=True).first()
    if organization is not None:
        _broadcast_on_commit(channels.rule_channel, channel_events.RuleFeedEvent(**{which: instance.pk, "change": change}), [f"rules_org_{organization}"])


@receiver(post_save, sender=models.Schedule)
def schedule_post_save(sender, instance: models.Schedule = None, created=None, **kwargs):
    _broadcast_rule("schedule", instance, "create" if created else "update")


@receiver(post_delete, sender=models.Schedule)
def schedule_post_delete(sender, instance: models.Schedule = None, **kwargs):
    _broadcast_rule("schedule", instance, "delete")


@receiver(post_save, sender=models.Trigger)
def trigger_post_save(sender, instance: models.Trigger = None, created=None, **kwargs):
    _broadcast_rule("trigger", instance, "create" if created else "update")


@receiver(post_delete, sender=models.Trigger)
def trigger_post_delete(sender, instance: models.Trigger = None, **kwargs):
    _broadcast_rule("trigger", instance, "delete")
