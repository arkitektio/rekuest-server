"""Transport port for the agent message queue.

The agent delivery path (backend ``broadcast`` -> agent socket) is a hand-rolled
redis queue rather than the Channels channel layer, on purpose: a message
pushed while the agent is offline persists in redis and survives reconnect,
which ``group_send`` to an empty group would drop.

``AgentQueue`` makes that swappable so the consumer can run against a real redis
in integration tests and against an in-memory fake in pure unit tests, without
monkeypatching the ``redis``/``redis.asyncio`` factories.
"""

import abc
import asyncio
import logging
from collections import defaultdict, deque
from typing import DefaultDict, Dict, Optional, Tuple

import redis
import redis.asyncio as aredis
from django.conf import settings

from facade import redis_keys

logger = logging.getLogger(__name__)

# Pre-namespace key shapes (``42_my_queue``). Still read once per connection so frames queued
# by a previous release are not stranded — see ``RedisAgentQueue.recover``. Remove after one release.
# Remove both of these — and the two places that read them, ``_adopt_legacy_lists`` and ``drop`` —
# one release after the namespacing ships (target: v3.2). Until then a frame queued by the previous
# release would be stranded under a key nothing looks at.
LEGACY_QUEUE_SUFFIX = "_my_queue"
LEGACY_PROCESSING_SUFFIX = "_processing"


def queue_key(agent_id: object) -> str:
    """The agent's FIFO backlog."""
    return redis_keys.key("agent", agent_id, "queue")


def processing_key(agent_id: object) -> str:
    """The per-agent in-flight list — scopes ``ack``/recovery to one agent."""
    return redis_keys.key("agent", agent_id, "processing")


# Reuse one sync connection pool per (host, port) across all the short-lived
# ``RedisAgentQueue`` instances that ``broadcast`` creates — otherwise every
# pushed message would open and tear down a fresh TCP connection.
_sync_pools: Dict[Tuple[str, int], "redis.ConnectionPool"] = {}


def _sync_pool(host: str, port: int) -> "redis.ConnectionPool":
    key = (host, port)
    pool = _sync_pools.get(key)
    if pool is None:
        pool = redis.ConnectionPool(host=host, port=port)
        _sync_pools[key] = pool
    return pool


class AgentQueue(abc.ABC):
    """A per-agent message queue: producers ``push``, the consumer ``pop``s."""

    @abc.abstractmethod
    def push(self, agent_id: str, message_json: str, *, priority: bool = False) -> None:
        """Enqueue a (already serialized) message for ``agent_id``.

        Synchronous: it is called from the backend / signal code (and from the
        classmethod ``AgentConsumer.broadcast``) which runs in a sync context.

        ``priority=True`` (used by probe traffic) makes the message the NEXT one popped,
        jumping the whole task backlog. Priority messages are LIFO among themselves — a
        probe Cancel pushed while its Assign is still queued pops first. That is
        acceptable by design: the server has already recorded the CANCELLING state, and
        agents must ignore a Cancel for an id they don't know (cancel races completion
        anyway); newest-first is the right order for hover-style traffic.
        """

    @abc.abstractmethod
    async def pop(self, agent_id: str) -> Optional[str]:
        """Block until a message is available for ``agent_id`` and return it.

        The message is moved to an in-flight holding area, NOT removed — the
        caller must ``ack`` it once it has been delivered. This send-then-ack
        ordering keeps delivery at-least-once: a crash between ``pop`` and
        ``ack`` leaves the message recoverable rather than lost.
        """

    @abc.abstractmethod
    async def ack(self, agent_id: str, message: str) -> None:
        """Acknowledge a message returned by ``pop`` (remove it from in-flight).

        ``agent_id`` scopes the removal to that agent's in-flight area.
        """

    @abc.abstractmethod
    async def recover(self, agent_id: str) -> int:
        """Return popped-but-never-acked messages to the head of the queue; returns how many.

        The other half of the at-least-once contract: ``pop`` parks a message in the in-flight
        area and only ``ack`` removes it, so whatever is still there when a new consumer takes
        over was popped by a consumer that died (or was cancelled, or lost its lease) before it
        acked — i.e. possibly never delivered. Must only be called by the connection that holds
        the agent's lease, before it starts popping. Order-preserving: the oldest in-flight
        message is delivered first again.
        """

    @abc.abstractmethod
    async def requeue(self, agent_id: str, message: str) -> None:
        """Hand a popped, UNDELIVERED message back: it becomes the next one popped.

        For a consumer that discovers, between ``pop`` and delivery, that it no longer holds
        the agent's lease: the frame belongs to the new holder's socket, not ours. Atomic, so a
        concurrent ``recover`` by the new holder can never turn it into two copies.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any underlying connections."""

    # Deliberately NOT on this port: ``drop`` (discard an agent's whole queue). It is a sync,
    # one-off administrative operation used when an agent stops being a websocket agent, and its
    # caller reaches for the concrete class. This port is the delivery path.


# How long one blocking pop waits before returning empty-handed. Finite on purpose: a
# ``timeout=0`` BLMOVE parks the consumer on the socket forever, so a half-dead redis connection
# (failover, a proxy that silently dropped us) is never noticed and the queue is never drained
# again, while the websocket heartbeat keeps the agent looking perfectly healthy.
POP_BLOCK_SECONDS = 5


class RedisAgentQueue(AgentQueue):
    """Redis-backed queue reproducing the original ``lpush``/``blmove`` flow."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._async_connection: Optional[aredis.Redis] = None
        self._legacy_checked = False

    @classmethod
    def from_settings(cls) -> "RedisAgentQueue":
        return cls(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)

    def push(self, agent_id: str, message_json: str, *, priority: bool = False) -> None:
        # Pooled connection: returned to the pool on use, not torn down per call.
        # The consumer BLMOVEs from the RIGHT (tail), so LPUSH = back of the FIFO line and
        # RPUSH = popped next — priority jumps the backlog atomically, with the
        # processing-list at-least-once semantics untouched.
        connection = redis.Redis(connection_pool=_sync_pool(self.host, self.port))
        if priority:
            connection.rpush(queue_key(agent_id), message_json)
        else:
            connection.lpush(queue_key(agent_id), message_json)

    def _connection(self) -> "aredis.Redis":
        if self._async_connection is None:
            # The socket timeout must outlast one blocking pop, or every idle pop would look
            # like a dead connection.
            self._async_connection = aredis.Redis(host=self.host, port=self.port, socket_timeout=POP_BLOCK_SECONDS + 5, socket_connect_timeout=5)
        return self._async_connection

    def drop(self, agent_id: str) -> int:
        """Discard everything queued for an agent that will never drain it again. Sync.

        For an agent that stopped being a websocket agent: nothing ``BLMOVE``s these lists any
        more, so frames in them are not late, they are lost. Returns how many were discarded;
        the caller makes their tasks redeliverable over the agent's new transport.
        """
        connection = redis.Redis(connection_pool=_sync_pool(self.host, self.port))
        keys = [queue_key(agent_id), processing_key(agent_id), f"{agent_id}{LEGACY_QUEUE_SUFFIX}", f"{agent_id}{LEGACY_PROCESSING_SUFFIX}"]
        dropped = sum(connection.llen(k) for k in keys)
        connection.delete(*keys)
        return dropped

    async def pop(self, agent_id: str) -> Optional[str]:
        # Move into this agent's processing list but leave it there; ``ack``
        # removes it only after the caller has delivered the message. Returns None when
        # nothing arrived within ``POP_BLOCK_SECONDS`` — the caller just pops again.
        task = await self._connection().blmove(queue_key(agent_id), processing_key(agent_id), timeout=POP_BLOCK_SECONDS, src="RIGHT", dest="LEFT")
        if task is None:
            return None
        return task.decode("utf-8")

    async def ack(self, agent_id: str, message: str) -> None:
        await self._connection().lrem(processing_key(agent_id), 0, message)

    async def recover(self, agent_id: str) -> int:
        # ``pop`` pushes onto the LEFT of the processing list, so its RIGHT end is the oldest
        # in-flight message; the queue is consumed from the RIGHT. Moving processing-LEFT →
        # queue-RIGHT one at a time therefore ends with the oldest message next in line.
        connection = self._connection()
        recovered = 0
        while await connection.lmove(processing_key(agent_id), queue_key(agent_id), src="LEFT", dest="RIGHT") is not None:
            recovered += 1
        if not self._legacy_checked:
            # Counted separately: adopting a previous release's frames is not the same event as
            # rescuing an undelivered one, and the caller logs the latter as a warning.
            adopted = await self._adopt_legacy_lists(agent_id)
            self._legacy_checked = True
            if adopted:
                logger.warning("Adopted %s message(s) queued for agent %s under the pre-namespacing keys", adopted, agent_id)
        return recovered

    async def _adopt_legacy_lists(self, agent_id: str) -> int:
        """Move frames a previous release queued under the un-namespaced keys into ours.

        ``LMOVE`` element by element, never ``RENAME``: new pushes may already sit in the
        namespaced queue and ``RENAME`` would clobber them. Legacy frames are older than anything
        queued since the upgrade, so they go to the head — in-flight ones first, then the backlog
        in its original order.
        """
        connection = self._connection()
        # The queue is consumed from the RIGHT, so whatever is pushed there LAST pops FIRST:
        # backlog first (newest → oldest), then the in-flight frames (newest → oldest), which
        # leaves the oldest in-flight frame next in line, exactly as before the upgrade.
        backlog = []
        while (frame := await connection.rpop(f"{agent_id}{LEGACY_QUEUE_SUFFIX}")) is not None:
            backlog.append(frame)  # oldest first
        for frame in reversed(backlog):
            await connection.rpush(queue_key(agent_id), frame)
        adopted = len(backlog)
        while await connection.lmove(f"{agent_id}{LEGACY_PROCESSING_SUFFIX}", queue_key(agent_id), src="LEFT", dest="RIGHT") is not None:
            adopted += 1
        return adopted

    async def requeue(self, agent_id: str, message: str) -> None:
        # One MULTI: only if the frame is still ours (still in-flight) does it go back to the
        # head. If the new lease holder's ``recover`` already took it, LREM finds nothing and we
        # must not push a second copy.
        await self._connection().eval(
            "if redis.call('LREM', KEYS[1], 1, ARGV[1]) > 0 then redis.call('RPUSH', KEYS[2], ARGV[1]) return 1 end return 0",
            2,
            processing_key(agent_id),
            queue_key(agent_id),
            message,
        )

    async def close(self) -> None:
        if self._async_connection is not None:
            await self._async_connection.aclose()
            self._async_connection = None


class InMemoryAgentQueue(AgentQueue):
    """In-process queue for unit tests — no redis, no network.

    Mirrors :class:`RedisAgentQueue` **including its in-flight area**, which matters more than it
    sounds: without one, `ack` is a no-op, `recover` can only return 0 and `requeue` cannot be
    conditional, so the at-least-once contract is untestable and three branches of
    ``listen_for_tasks`` (idle-recover, ack-failure, fenced-requeue) are unreachable. A regression
    that turned `requeue` into a double delivery would have passed every unit test.

    The list semantics are the redis ones: a deque per agent, ``appendleft`` = FIFO tail,
    ``append`` = the next one popped (redis ``RPUSH``), popped from the right. ``pop`` moves the
    frame into the in-flight deque rather than removing it, and only ``ack`` drops it.

    What this still cannot mirror is redis *durability*: state lives in the object. The
    at-least-once behaviour that survives a process restart is pinned by the redis-backed tests in
    ``tests/agent/test_delivery.py``, not here.
    """

    #: How long ``pop`` blocks before returning None, mirroring ``POP_BLOCK_SECONDS``. Tiny by
    #: default so a test that exercises the idle path does not pay five seconds for it.
    pop_timeout: float = 0.05

    def __init__(self, pop_timeout: float | None = None) -> None:
        self._queues: DefaultDict[str, "deque[str]"] = defaultdict(deque)
        self._inflight: DefaultDict[str, "deque[str]"] = defaultdict(deque)
        self._tokens: DefaultDict[str, "asyncio.Queue[None]"] = defaultdict(asyncio.Queue)
        if pop_timeout is not None:
            self.pop_timeout = pop_timeout

    def _offer(self, agent_id: str, message_json: str, *, next_up: bool) -> None:
        """Put a frame on the queue and wake one waiter. ``next_up`` = redis ``RPUSH``."""
        if next_up:
            self._queues[agent_id].append(message_json)
        else:
            self._queues[agent_id].appendleft(message_json)
        self._tokens[agent_id].put_nowait(None)

    def push(self, agent_id: str, message_json: str, *, priority: bool = False) -> None:
        self._offer(agent_id, message_json, next_up=priority)

    async def pop(self, agent_id: str) -> Optional[str]:
        try:
            await asyncio.wait_for(self._tokens[agent_id].get(), timeout=self.pop_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        queue = self._queues[agent_id]
        if not queue:
            return None  # a token whose frame another consumer already took
        message = queue.pop()
        # Parked, not removed: ``ack`` is what removes it. ``appendleft`` mirrors redis moving into
        # the LEFT of the processing list, which is what makes the RIGHT end the oldest.
        self._inflight[agent_id].appendleft(message)
        return message

    async def ack(self, agent_id: str, message: str) -> None:
        try:
            self._inflight[agent_id].remove(message)
        except ValueError:
            pass  # already acked, or recovered by a newer lease holder — both benign

    async def recover(self, agent_id: str) -> int:
        inflight = self._inflight[agent_id]
        recovered = 0
        # Newest first onto the front of the queue, so the OLDEST in-flight frame ends up next.
        while inflight:
            self._offer(agent_id, inflight.popleft(), next_up=True)
            recovered += 1
        return recovered

    async def requeue(self, agent_id: str, message: str) -> None:
        # Conditional, exactly like the redis Lua script: only a frame still in flight goes back.
        # If a new lease holder's ``recover`` already took it, pushing again would duplicate it.
        try:
            self._inflight[agent_id].remove(message)
        except ValueError:
            return
        self._offer(agent_id, message, next_up=True)

    def drop(self, agent_id: str) -> int:
        """Discard everything queued and in flight for an agent (see the redis counterpart)."""
        dropped = len(self._queues[agent_id]) + len(self._inflight[agent_id])
        self._queues[agent_id].clear()
        self._inflight[agent_id].clear()
        while not self._tokens[agent_id].empty():
            self._tokens[agent_id].get_nowait()
        return dropped

    async def close(self) -> None:
        return None
