"""Reading the redis-held state of ephemeral Probes.

takt writes it (``takt/crates/facade/src/probes/store.rs``): one hash per
probe and a per-caller in-flight counter, on the redis the agent queues use, expiring on their
own. This server only reads, for the ``probe`` query, the ``probeStats`` query and the probe
subscription's catch-up:

    probe:{id}                 HASH   agent, caller, user, org, action, impl, iface, ref,
                                     kind, seq, done, last_returns, err, created
    probe-inflight:{caller_pk} STR    in-flight counter (per-caller backpressure)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from dataclasses import dataclass

import redis
import redis.asyncio as aredis
from django.conf import settings

from facade import redis_keys
from facade.takt_api import ProbeState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeCounts:
    """Live probe counts: the whole instance's, and one caller's against its cap."""

    total_live: int
    my_inflight: int
    max_inflight: int


def probe_max_inflight_per_caller() -> int:
    return int(settings.PROBE_MAX_INFLIGHT_PER_CALLER)


# All probe keys live under the service's redis namespace (``facade.redis_keys``). Probe state
# from before the namespacing simply expires with its TTL — probes are hover-grade.
def _call_key(probe_id: str) -> str:
    return redis_keys.key("probe", probe_id)


def _inflight_key(caller_pk: int | str) -> str:
    return redis_keys.key("probe-inflight", caller_pk)


# One decoding pool per (host, port), mirroring the agent queue's pooling so the
# short-lived per-request store objects don't churn TCP connections.
_sync_pools: dict[tuple[str, int], "redis.ConnectionPool"] = {}


def _sync_pool(host: str, port: int) -> "redis.ConnectionPool":
    key = (host, port)
    pool = _sync_pools.get(key)
    if pool is None:
        pool = redis.ConnectionPool(host=host, port=port, decode_responses=True)
        _sync_pools[key] = pool
    return pool


class ProbeStore:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        # Async clients are bound to the event loop they were created on, so cache one per
        # loop (weak keys: a finished loop — e.g. per-test loops — drops its client). In a
        # daphne worker there is exactly one loop, so this is a single long-lived client.
        self._async_connections: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aredis.Redis]" = weakref.WeakKeyDictionary()

    @classmethod
    def from_settings(cls) -> "ProbeStore":
        return cls(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)

    # ------------------------------------------------------------------ #
    # sync — the GraphQL mutation path
    # ------------------------------------------------------------------ #

    def _sync(self) -> "redis.Redis":
        return redis.Redis(connection_pool=_sync_pool(self.host, self.port))

    def get(self, probe_id: str) -> ProbeState | None:
        state = self._sync().hgetall(_call_key(probe_id))
        # redis-py types a command as its value or an awaitable of it; this client is the sync one.
        if not isinstance(state, dict):
            raise TypeError("The sync redis client answered with an awaitable")
        return ProbeState.model_validate({**state, "id": probe_id}) if state else None

    # ------------------------------------------------------------------ #
    # async — the message-router handler path
    # ------------------------------------------------------------------ #

    def _async(self) -> "aredis.Redis":
        loop = asyncio.get_running_loop()
        connection = self._async_connections.get(loop)
        if connection is None:
            connection = aredis.Redis(host=self.host, port=self.port, decode_responses=True)
            self._async_connections[loop] = connection
        return connection

    async def aget(self, probe_id: str) -> ProbeState | None:
        pending = self._async().hgetall(_call_key(probe_id))
        state = await pending if inspect.isawaitable(pending) else pending
        return ProbeState.model_validate({**state, "id": probe_id}) if state else None

    def stats_sync(self, caller_pk: int | str) -> ProbeCounts:
        """Live probe counts for the stats query.

        ``total_live`` counts the self-expiring probe hashes via SCAN — chosen over a
        maintained counter because TTL expiry never decrements a counter (no keyspace
        notifications are wired), so a counter drifts monotonically; the keyspace is
        small and stats is a rare admin query. ``probe:p-*`` cannot collide with the
        ``probe:agent:*`` / ``probe:inflight:*`` index keys.
        """
        connection = self._sync()
        total = 0
        for _ in connection.scan_iter(match=_call_key("p-*"), count=500):
            total += 1
        raw = connection.get(_inflight_key(caller_pk))
        inflight = max(0, int(raw)) if isinstance(raw, (str, bytes, int)) else 0
        return ProbeCounts(total_live=total, my_inflight=inflight, max_inflight=probe_max_inflight_per_caller())


_default_store: ProbeStore | None = None


def get_probe_store() -> ProbeStore:
    """The process-wide store (lazy so importing this module needs no settings)."""
    global _default_store
    if _default_store is None:
        _default_store = ProbeStore.from_settings()
    return _default_store
