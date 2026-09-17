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
from collections import defaultdict, deque
from typing import DefaultDict, Dict, Optional, Tuple

import redis
import redis.asyncio as aredis
from django.conf import settings

from facade import redis_keys

# Pre-namespace key shapes (``42_my_queue``). Still read once per connection so frames queued
# by a previous release are not stranded — see ``RedisAgentQueue.recover``. Remove after one release.
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
            recovered += await self._adopt_legacy_lists(agent_id)
            self._legacy_checked = True
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

    Mirrors the redis list semantics: a deque holds the messages (append-left = FIFO
    tail, append-right = priority head, pop from the right) and a token queue provides
    the blocking wait.
    """

    def __init__(self) -> None:
        self._queues: DefaultDict[str, "deque[str]"] = defaultdict(deque)
        self._tokens: DefaultDict[str, "asyncio.Queue[None]"] = defaultdict(asyncio.Queue)

    def push(self, agent_id: str, message_json: str, *, priority: bool = False) -> None:
        if priority:
            self._queues[agent_id].append(message_json)
        else:
            self._queues[agent_id].appendleft(message_json)
        self._tokens[agent_id].put_nowait(None)

    async def pop(self, agent_id: str) -> Optional[str]:
        await self._tokens[agent_id].get()
        return self._queues[agent_id].pop()

    async def ack(self, agent_id: str, message: str) -> None:
        # ``pop`` already removed the item; nothing to do.
        return None

    async def recover(self, agent_id: str) -> int:
        # No in-flight area to recover from.
        return 0

    async def requeue(self, agent_id: str, message: str) -> None:
        # ``pop`` removed it; put it back as the next one popped.
        self._queues[agent_id].append(message)
        self._tokens[agent_id].put_nowait(None)

    async def close(self) -> None:
        return None
