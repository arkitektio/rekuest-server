import logging

from authentikate.models import Organization
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from kante.channel import Channel
from pydantic import BaseModel

from facade import channel_events, channels, models

logger = logging.getLogger(__name__)


def _broadcast_on_commit[Event: BaseModel](channel: Channel[Event], event: Event, topics: list[str] | None = None) -> None:
    """Fan an event out only once the surrounding transaction commits.

    Signals fire while the writing transaction is still open (e.g. the atomic
    ``implement_agent`` reconcile). Broadcasting immediately would publish events for rows
    that may still roll back, and serializes the channel-layer work inside the lock
    window. Outside a transaction ``on_commit`` runs the callback immediately, so
    non-transactional paths are unaffected.
    """
    transaction.on_commit(lambda: channel.broadcast(event, topics))


def broadcast_agent_update(agent: models.Agent, created: bool = False) -> None:
    """Refresh the agent feeds — callable directly when a change needs no row write (an M2M
    edit), so nobody has to ``save()`` an agent merely to fire this signal."""
    _broadcast_on_commit(
        channels.agent_updated_channel,
        channel_events.AgentEvent(create=agent.pk) if created else channel_events.AgentEvent(update=agent.pk),
        [f"agents_for_{agent.organization_id}"],
    )


@receiver(post_save, sender=models.Agent)
def agent_post_save(sender: type[models.Agent], instance: models.Agent, created: bool, **kwargs: object) -> None:
    broadcast_agent_update(instance, created=created)


@receiver(post_save, sender=models.Task)
def task_post_save(sender: type[models.Task], instance: models.Task, created: bool, **kwargs: object) -> None:
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
def implementation_post_save(sender: type[models.Implementation], instance: models.Implementation, created: bool, **kwargs: object) -> None:
    # Two audiences: the per-implementation detail feed (implementation_change) and the
    # per-agent list feed (implementations) — every event reaches both.
    topics = [f"implementation_{instance.pk}", f"implementations_agent_{instance.agent_id}"]
    event = channel_events.ImplementationEvent(create=instance.pk) if created else channel_events.ImplementationEvent(update=instance.pk)
    _broadcast_on_commit(channels.new_implementation_channel, event, topics)


@receiver(post_delete, sender=models.Implementation)
def implementation_post_del(sender: type[models.Implementation], instance: models.Implementation, **kwargs: object) -> None:
    _broadcast_on_commit(
        channels.new_implementation_channel,
        channel_events.ImplementationEvent(delete=instance.pk),
        [f"implementation_{instance.pk}", f"implementations_agent_{instance.agent_id}"],
    )


@receiver(post_save, sender=Organization)
def organization_post_save(sender: type[Organization], instance: Organization, created: bool, **kwargs: object) -> None:
    """A new organization gets every hook agent on the fly (an organization takt was
    first to see has no such moment; the provisioning pass gives it its agents)."""
    if created:
        from facade import hook_agents

        transaction.on_commit(lambda: hook_agents.provision_new_organization(instance))


def _broadcast_rule(rule: models.Schedule | models.Trigger, change: str) -> None:
    """Tell the organization's rule feed. Bookkeeping takt writes with its own SQL is published by takt."""
    organization = models.Caller.objects.filter(pk=rule.caller_id).values_list("organization_id", flat=True).first()
    if organization is None:
        return
    if isinstance(rule, models.Schedule):
        event = channel_events.RuleFeedEvent(schedule=rule.pk, change=change)
    else:
        event = channel_events.RuleFeedEvent(trigger=rule.pk, change=change)
    _broadcast_on_commit(channels.rule_channel, event, [f"rules_org_{organization}"])


@receiver(post_save, sender=models.Schedule)
def schedule_post_save(sender: type[models.Schedule], instance: models.Schedule, created: bool, **kwargs: object) -> None:
    _broadcast_rule(instance, "create" if created else "update")


@receiver(post_delete, sender=models.Schedule)
def schedule_post_delete(sender: type[models.Schedule], instance: models.Schedule, **kwargs: object) -> None:
    _broadcast_rule(instance, "delete")


@receiver(post_save, sender=models.Trigger)
def trigger_post_save(sender: type[models.Trigger], instance: models.Trigger, created: bool, **kwargs: object) -> None:
    _broadcast_rule(instance, "create" if created else "update")


@receiver(post_delete, sender=models.Trigger)
def trigger_post_delete(sender: type[models.Trigger], instance: models.Trigger, **kwargs: object) -> None:
    _broadcast_rule(instance, "delete")
