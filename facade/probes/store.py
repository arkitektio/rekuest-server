"""Redis-held state for ephemeral Probes.

One hash per probe plus two small indexes, on the same redis the agent queue uses:

    probe:{id}                 HASH   agent, caller, user, org, action, impl, iface, ref,
                                     kind, seq, done, last_returns, err, created
    probe:agent:{agent_pk}     SET    live probe ids (fail-fast fan-out on agent death)
    probe:inflight:{caller_pk} STR    in-flight counter (per-caller backpressure)

The hash doubles as the concurrency primitive that ``select_for_update`` provides for
Tasks: the terminal transition is claimed with ``HSETNX done <kind>`` — exactly one winner
across all daphne processes; losers treat their (resent) terminal report as a dup and
drop it. ``seq`` is a per-probe ``HINCRBY`` counter, monotonic across processes, and takes
the role the TaskEvent PK plays for persisted tasks (the caller protocol's ordering and
dedup key).

Everything expires: ``PROBE_TTL_SECONDS`` while live (refreshed on every write), reduced
to ``PROBE_LINGER_SECONDS`` once terminal so a late subscriber can still read the outcome.
Expiry IS the garbage collector — there is deliberately no sweep.

Sync methods serve the GraphQL mutation path (sync resolvers, pooled connections like the
agent queue); async methods serve the message-router handlers on the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any, Dict, Optional, Tuple

import redis
import redis.asyncio as aredis
from django.conf import settings
from django.utils import timezone

from facade import redis_keys

logger = logging.getLogger(__name__)


def probe_ttl_seconds() -> int:
    return int(getattr(settings, "PROBE_TTL_SECONDS", 3600))


def probe_linger_seconds() -> int:
    return int(getattr(settings, "PROBE_LINGER_SECONDS", 300))


def probe_max_inflight_per_caller() -> int:
    return int(getattr(settings, "PROBE_MAX_INFLIGHT_PER_CALLER", 32))


# All probe keys live under the service's redis namespace (``facade.redis_keys``). Probe state
# from before the namespacing simply expires with its TTL — probes are hover-grade.
def _call_key(probe_id: str) -> str:
    return redis_keys.key("probe", probe_id)


def _agent_index_key(agent_pk: int | str) -> str:
    return redis_keys.key("probe-agent", agent_pk)


def _inflight_key(caller_pk: int | str) -> str:
    return redis_keys.key("probe-inflight", caller_pk)


# One decoding pool per (host, port), mirroring the agent queue's pooling so the
# short-lived per-request store objects don't churn TCP connections.
_sync_pools: Dict[Tuple[str, int], "redis.ConnectionPool"] = {}


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

    def try_acquire_slot(self, caller_pk: int | str) -> bool:
        """Take one in-flight slot for this caller, or refuse at the cap.

        The counter expires with the probe TTL so a crashed handler can never wedge a
        caller's budget permanently — worst case the cap self-heals one TTL later.
        """
        connection = self._sync()
        key = _inflight_key(caller_pk)
        with connection.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, probe_ttl_seconds())
            count = pipe.execute()[0]
        if int(count) > probe_max_inflight_per_caller():
            connection.decr(key)
            return False
        return True

    def release_slot_sync(self, caller_pk: int | str) -> None:
        """Give the slot back on a create that failed after acquiring it."""
        self._sync().decr(_inflight_key(caller_pk))

    def create(
        self,
        probe_id: str,
        *,
        agent_pk: int,
        caller_pk: int,
        user_sub: str,
        org_slug: str,
        action_pk: int,
        implementation_pk: int,
        interface: str,
        reference: Optional[str],
        origin: str = "graphql",
    ) -> Dict[str, str]:
        """Write the probe hash and index it under its agent. Returns the stored state.

        ``origin`` records who fired the probe: ``"graphql"`` (a client via the mutation)
        or ``"agent"`` (over the socket via ProbeRequest) — agent-origin probes mirror
        their events onto the requester's caller topic.
        """
        state = {
            "agent": str(agent_pk),
            "caller": str(caller_pk),
            "user": user_sub,
            "org": org_slug,
            "action": str(action_pk),
            "impl": str(implementation_pk),
            "iface": interface,
            "ref": reference or "",
            "kind": "QUEUED",
            "seq": "0",
            "origin": origin,
            "created": timezone.now().isoformat(),
        }
        connection = self._sync()
        with connection.pipeline(transaction=True) as pipe:
            pipe.hset(_call_key(probe_id), mapping=state)
            pipe.expire(_call_key(probe_id), probe_ttl_seconds())
            pipe.sadd(_agent_index_key(agent_pk), probe_id)
            pipe.execute()
        return state

    def get(self, probe_id: str) -> Optional[Dict[str, str]]:
        state = self._sync().hgetall(_call_key(probe_id))
        return state or None

    def record_nonterminal_sync(self, probe_id: str, kind: str) -> Optional[Tuple[int, Optional[str], str]]:
        """Sync twin of :meth:`record_nonterminal` for the mutation path (e.g. CANCELLING).

        Returns ``(seq, caller, origin)`` like its async twin.
        """
        connection = self._sync()
        key = _call_key(probe_id)
        state = connection.hmget(key, "done")
        if state == [None] and not connection.exists(key):
            return None
        if state[0]:
            return None
        with connection.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "seq", 1)
            pipe.hset(key, "kind", kind)
            pipe.expire(key, probe_ttl_seconds())
            pipe.hmget(key, "caller", "origin")
            results = pipe.execute()
        caller, origin = results[-1]
        return int(results[0]), caller, origin or "graphql"

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

    async def aget(self, probe_id: str) -> Optional[Dict[str, str]]:
        state = await self._async().hgetall(_call_key(probe_id))
        return state or None

    async def record_nonterminal(
        self,
        probe_id: str,
        kind: str,
        *,
        returns: Optional[dict] = None,
    ) -> Optional[Tuple[int, Optional[str], str]]:
        """Record a non-terminal event; returns ``(seq, caller, origin)``, or None for an
        unknown/expired or already-terminal probe (the event is then dropped — nobody is
        listening)."""
        connection = self._async()
        key = _call_key(probe_id)
        # EXISTS first so an expired probe doesn't get resurrected as a stub hash by
        # HINCRBY. The check-then-write race with expiry only ever creates a stub that
        # the trailing EXPIRE removes again — never a live-looking probe.
        state = await connection.hmget(key, "done")
        if state == [None] and not await connection.exists(key):
            return None
        if state[0]:
            return None  # terminal already claimed — a late/racing non-terminal is noise
        async with connection.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "seq", 1)
            pipe.hset(key, "kind", kind)
            if returns is not None:
                pipe.hset(key, "last_returns", json.dumps(returns))
            pipe.expire(key, probe_ttl_seconds())
            pipe.hmget(key, "caller", "origin")
            results = await pipe.execute()
        caller, origin = results[-1]
        return int(results[0]), caller, origin or "graphql"

    async def claim_terminal(
        self,
        probe_id: str,
        kind: str,
        *,
        error: Optional[str] = None,
    ) -> Optional[Tuple[int, Dict[str, str]]]:
        """Claim the terminal transition. Returns ``(seq, state)`` for the single winner,
        None for losers (dup terminal reports) and unknown/expired probes."""
        connection = self._async()
        key = _call_key(probe_id)
        if not await connection.exists(key):
            return None
        if not await connection.hsetnx(key, "done", kind):
            return None  # another process (or a resent report) already closed this probe
        async with connection.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "seq", 1)
            pipe.hset(key, "kind", kind)
            if error is not None:
                pipe.hset(key, "err", error)
            pipe.expire(key, probe_linger_seconds())
            pipe.hgetall(key)
            results = await pipe.execute()
        seq, state = int(results[0]), results[-1]
        await connection.srem(_agent_index_key(state.get("agent", "")), probe_id)
        if state.get("caller"):
            await connection.decr(_inflight_key(state["caller"]))
        return seq, state

    def stats_sync(self, caller_pk: int | str) -> Dict[str, int]:
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
        inflight = max(0, int(raw)) if raw is not None else 0
        return {
            "total_live": total,
            "my_inflight": inflight,
            "max_inflight": probe_max_inflight_per_caller(),
        }

    async def live_calls_for_agent(self, agent_pk: int | str) -> list[str]:
        return sorted(await self._async().smembers(_agent_index_key(agent_pk)))

    async def forget_agent_calls(self, agent_pk: int | str, probe_ids: list[str]) -> None:
        """Remove exactly these ids from the agent's live-probe index.

        Never ``DELETE`` the whole set: the agent may already have reconnected — to this or to
        another replica — and registered NEW probes in it while the old connection's teardown
        was still running. Dropping those from the index means no later disconnect fails them,
        their terminal claim never runs, and the caller's in-flight slots leak until it is
        refused new probes altogether.
        """
        if probe_ids:
            await self._async().srem(_agent_index_key(agent_pk), *probe_ids)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        connection = self._async_connections.pop(loop, None)
        if connection is not None:
            await connection.aclose()


_default_store: Optional[ProbeStore] = None


def get_probe_store() -> ProbeStore:
    """The process-wide store (lazy so importing this module needs no settings)."""
    global _default_store
    if _default_store is None:
        _default_store = ProbeStore.from_settings()
    return _default_store
