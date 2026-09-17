"""The one place that knows *how to reach* a participant.

Two best-effort notifiers over the authoritative DB rows:

- :func:`deliver_to_agent` — a single ToAgent command to one agent: redis queue for a
  WEBSOCKET agent, HMAC-signed POST for a WEBHOOK HookAgent.
- :func:`publish_task_event` — fan a persisted ``TaskEvent`` out to its
  caller: the channel layer (GraphQL subscription + live WS forward) and, if the caller is a
  HookAgent, a webhook POST.

Both run only AFTER the relevant row is persisted, so a failed notification is recoverable
from the DB — the real-time layer never has to be reliable, only prompt.
"""

from __future__ import annotations

import logging
import time

from facade import caller_events, channel_events, channels, enums, hooks, messages, models
from facade.consumers.agent_queue import RedisAgentQueue

logger = logging.getLogger(__name__)

# Process-local caches are per BACKEND: with several of them, each holds its own opinion for a
# full TTL, and which one a request lands on is the load balancer's coin toss. So only things
# that cannot change are cached long (a caller's organization), a merely-advisory lookup is
# cached briefly (the caller's webhook: the hot path is "none", and a miss costs a best-effort
# mirror POST, never the persisted event) — and the ROUTING of a command to an agent is not
# cached at all. It used to be, for 60 s: after an agent flipped WEBSOCKET→WEBHOOK a backend
# with a warm entry kept pushing its commands into a redis list no socket would ever drain
# again — not late, lost — while its neighbours delivered correctly.
_DELIVERY_CACHE_TTL = 60.0
_WEBHOOK_LOOKUP_TTL = 5.0
_DELIVERY_CACHE_MAX = 4096
_caller_webhook_cache: dict[int, tuple[float, models.Agent | None]] = {}


def _cache_put(cache: dict, key: int, value, ttl: float = _DELIVERY_CACHE_TTL) -> None:
    if len(cache) >= _DELIVERY_CACHE_MAX:
        cache.clear()
    cache[key] = (time.monotonic() + ttl, value)


def get_agent_for_delivery(agent_id: int) -> models.Agent:
    """The slim Agent row needed to route a delivery — always read fresh (one PK lookup)."""
    return models.Agent.objects.only("id", "kind", "hook_url", "hook_url_secret").get(id=agent_id)


def forget_agent_routing(agent: models.Agent) -> None:
    """Drop what this process cached about ``agent``'s webhook identity (called on save)."""
    for caller_id, (_, cached) in list(_caller_webhook_cache.items()):
        if cached is None or cached.pk == agent.pk:
            _caller_webhook_cache.pop(caller_id, None)


def deliver_to_agent(agent: models.Agent, message: messages.ToAgentMessage, *, priority: bool = False) -> bool:
    """Send one ToAgent message to ``agent`` over its transport (queue or webhook).

    Returns whether the transport accepted it: the webhook answered 2xx, or the message is in
    the agent's redis queue (which also holds it for an offline agent). A redis failure still
    raises. Dispatch paths record a ``False``/raise on the task (``dispatched_at=NULL``) so the
    pickup watchdog retries it — a webhook that was down is otherwise never heard from again.

    ``priority`` (probe traffic) jumps the agent's queued backlog; the webhook transport
    has no queue to jump, so it is ignored there.
    """
    body = message.model_dump_json()
    if agent.kind == enums.AgentKind.WEBHOOK.value:
        return hooks.deliver_to_hook(agent, body)
    RedisAgentQueue.from_settings().push(str(agent.pk), body, priority=priority)
    return True


def publish_task_event(event: models.TaskEvent) -> None:
    """Fan a persisted task event out to its caller (channel layer + webhook).

    Handlers that already hold the Task row acreate with ``task=x`` so the FK cache is
    warm here (zero SELECTs); the fire-and-forget events (Yield/Log/Progress) acreate by
    id, so this loads ONE slim row. The caller's organization comes from a TTL cache.
    """
    if models.TaskEvent.task.is_cached(event):
        task = event.task
    else:
        task = models.Task.objects.only("id", "caller_id", "root_id").get(pk=event.task_id)
    caller_id = task.caller_id
    if not caller_id:
        return
    # The live WS forward (agent socket) consumes every caller event, root and child alike, on
    # ``task_caller_{caller_id}``. Root-task events additionally feed the slim GraphQL change
    # feeds (mytasks / tasks), which fan out to both the caller's feed and the org-wide feed.
    topics = [f"task_caller_{caller_id}"]
    if task.root_id is None:
        topics += [
            f"root_tasks_caller_{caller_id}",
            f"root_tasks_org_{_get_org_for_caller(caller_id)}",
        ]
    channels.task_event_channel.broadcast(
        channel_events.TaskEventCreatedEvent(event=channel_events.TaskEventPayload.from_event(event)),  # pyright: ignore[reportCallIssue]  # pydantic Field(None) default
        topics,
    )
    _deliver_caller_event_to_webhook(event, caller_id)


_caller_org_cache: dict[int, tuple[float, int]] = {}


def _get_org_for_caller(caller_id: int) -> int:
    """The caller's organization id, behind the TTL cache — a caller never changes org."""
    hit = _caller_org_cache.get(caller_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    organization_id = models.Caller.objects.values_list("organization_id", flat=True).get(pk=caller_id)
    _cache_put(_caller_org_cache, caller_id, organization_id)
    return organization_id


def _get_webhook_agent_for_caller(caller_id: int) -> models.Agent | None:
    """The caller's HookAgent, if any, behind the TTL cache — almost every caller has none."""
    hit = _caller_webhook_cache.get(caller_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    caller = models.Caller.objects.only("id", "client_id", "user_id", "organization_id").get(pk=caller_id)
    agent = (
        models.Agent.objects.filter(
            client_id=caller.client_id,
            user_id=caller.user_id,
            organization_id=caller.organization_id,
            kind=enums.AgentKind.WEBHOOK.value,
        )
        .exclude(hook_url__isnull=True)
        .exclude(hook_url="")
        .first()
    )
    _cache_put(_caller_webhook_cache, caller_id, agent, ttl=_WEBHOOK_LOOKUP_TTL)
    return agent


def _deliver_caller_event_to_webhook(event: models.TaskEvent, caller_id: int) -> None:
    """If the task's caller is a HookAgent, POST the …Event mirror to its hook_url."""
    agent = _get_webhook_agent_for_caller(caller_id)
    if agent is None:
        return
    # A Django model satisfies EventLike at runtime, but pyright can't see through the
    # TextChoicesField descriptor to verify it structurally (needs a mypy plugin).
    message = caller_events.build_execution_event(event)  # pyright: ignore[reportArgumentType]
    if message is not None:
        hooks.deliver_to_hook(agent, message.model_dump_json())
