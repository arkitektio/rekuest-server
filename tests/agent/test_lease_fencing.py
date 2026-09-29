"""The executor write-lease and its fencing token — one test per way liveness used to lie.

Liveness is read as ``connected AND a fresh heartbeat`` (``facade.liveness``), and that read
predicate is fine: ``connected=False`` is a definitive negative, ``connected=True`` is merely
not-yet-refuted and the lease is what makes it trustworthy. What was NOT fine was the *write*
discipline, and each class below pins one of the resulting lies:

* a displaced connection going on renewing the lease it no longer owns,
* a stalled worker resurrecting itself after the sweep already failed its work,
* two concurrent registrations both being handed the same in-flight work,
* two concurrent sweeps both emitting a terminal event for the same task.

These drive ``ModelPersistBackend`` directly (a fresh instance per test isolates its in-memory
grace registries); the socket-level half lives in ``test_protocol_unit`` / ``test_conflict``.
"""

import asyncio
import datetime
import threading

import pytest
from django.utils import timezone

from facade import enums, liveness
from facade.consumers.agent_protocol import RegisteredSession
from facade.consumers.agent_queue import InMemoryAgentQueue
from facade.models import Agent, Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.factories import build_task

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


def _grace(settings, value):
    settings.REKUEST_GRACE = {"DEFAULT": value, "PHYSICAL": value}


def _session(agent, backend, *, lease_epoch) -> RegisteredSession:
    """A post-register session bound to the REAL backend, with the transport stubbed out.

    Lets a test drive ``on_agent_heartbeat`` against Postgres — the protocol-level fakes in
    ``test_protocol_unit`` prove the branch is taken, this proves what it writes (or doesn't).
    """

    async def _noop(*args, **kwargs):
        return None

    return RegisteredSession(
        agent=agent,
        session_id="S1",
        caller_id="caller-1",
        connection_id="conn-1",
        lease_epoch=lease_epoch,
        backend=backend,
        queue=InMemoryAgentQueue(),
        send_to_agent_message=_noop,
        send=_noop,
        close=_noop,
        heartbeat_interval=10.0,
        heartbeat_timeout=5.0,
    )


async def _expire_lease(agent_id):
    """Push ``last_seen`` outside the stale window without touching ``connected``.

    Models the state a crashed or wedged worker leaves behind: stuck ``connected=True`` with a
    lease nobody is renewing.
    """
    stale = timezone.now() - datetime.timedelta(seconds=liveness.stale_after_seconds() + 5)
    await Agent.objects.filter(pk=agent_id).aupdate(last_seen=stale)


class TestOnlyTheLeaseHolderRenews:
    async def test_heartbeat_renews_the_lease(self):
        # The connection holding the lease renews it, so a live agent never goes stale. (The
        # old "an observer's heartbeat forges executor liveness" hole is now structurally
        # impossible: there is no non-agent connection kind left to hold a socket open.)
        task = await build_task("exec-renews")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        claim = await backend.on_agent_connected(agent_id, "exec-1", session_id="S1")
        await _expire_lease(agent_id)

        agent = await Agent.objects.aget(pk=agent_id)
        session = _session(agent, backend, lease_epoch=claim.epoch)
        session.heartbeat_future = asyncio.get_event_loop().create_future()
        await session.on_agent_heartbeat()

        after = await Agent.objects.aget(pk=agent_id)
        assert liveness.agent_is_live(after.connected, after.last_seen) is True
        assert await backend.reconcile_stale_agents() == 0

    async def test_unrenewed_lease_goes_stale_and_is_swept(self):
        # And with nobody renewing, the lease expires on its own — no writer required.
        task = await build_task("exec-norenew")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "exec-1", session_id="S1")
        await _expire_lease(agent_id)

        agent = await Agent.objects.aget(pk=agent_id)
        assert liveness.agent_is_live(agent.connected, agent.last_seen) is False
        assert await backend.reconcile_stale_agents() == 1


class TestReconcileUsesTheOneLivenessPredicate:
    async def test_stuck_connected_but_stale_agent_still_reconciles(self, settings):
        # The bail-out in ``reconcile_orphaned_executor_work`` must ask ``agent_is_live``, not
        # ``connected``. A crashed worker leaves ``connected`` stuck True with an expired lease;
        # guarding on the raw flag made this a silent no-op and its work stayed in-flight
        # forever. Called directly here — i.e. WITHOUT the sweep revoking first, which is the
        # sequencing that used to mask the bug.
        _grace(settings, 0)
        task = await build_task("reconcile-stale", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)  # crashed: connected stuck True, lease expired

        agent = await Agent.objects.aget(pk=agent_id)
        assert agent.connected is True, "precondition: the flag is still stuck True"

        await backend.reconcile_orphaned_executor_work(agent_id)

        kinds = [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)]
        assert enums.TaskEventKind.LOST in kinds

    async def test_live_agent_is_still_a_no_op(self):
        # The other direction: a genuinely live agent's work must never be failed — that is the
        # reclaim guarantee the grace timer depends on.
        task = await build_task("reconcile-live", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")

        await backend.reconcile_orphaned_executor_work(agent_id)

        assert [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)] == []
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.is_done is False


class TestDisplacedConnectionIsFenced:
    async def test_displaced_connection_cannot_renew(self):
        # A force-takeover bumps the epoch, so the incumbent's next heartbeat renewal matches
        # no row. This is what makes ``kick_others`` an optimization rather than a correctness
        # dependency: a channel-layer kick that never lands no longer leaves the old connection
        # refreshing the lease (and draining the queue) behind the new owner's back.
        task = await build_task("fence-displace")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        first = await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        assert await backend.renew_agent_lease(agent_id, first.epoch) is True

        second = await backend.on_agent_connected(agent_id, "c2", session_id="S1", force=True)
        assert second.claimed and second.epoch != first.epoch
        assert second.displaced_incumbent is True

        assert await backend.renew_agent_lease(agent_id, first.epoch) is False  # fenced
        assert await backend.renew_agent_lease(agent_id, second.epoch) is True

    async def test_displaced_renewal_does_not_move_last_seen(self):
        # Not just "returns False" — it must not write. A losing renewal that still refreshed
        # ``last_seen`` would keep a dead agent looking live no matter what the return value said.
        task = await build_task("fence-nowrite")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        first = await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_connected(agent_id, "c2", session_id="S1", force=True)
        await _expire_lease(agent_id)

        before = (await Agent.objects.aget(pk=agent_id)).last_seen
        assert await backend.renew_agent_lease(agent_id, first.epoch) is False
        after = (await Agent.objects.aget(pk=agent_id)).last_seen
        assert after == before


class TestSweepRevocationIsFinal:
    async def test_late_heartbeat_after_sweep_does_not_resurrect(self, settings):
        # The stalled-worker sequence: the sweep revokes the lease and fails the in-flight work,
        # then the worker's event loop resumes and answers one more heartbeat. Under the old
        # ``connected = True; last_seen = now()`` write that resurrected the agent as live with
        # its work already terminally failed. The epoch bump makes the revocation stick.
        _grace(settings, 0)
        task = await build_task("revoke-final", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        claim = await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)

        assert await backend.reconcile_stale_agents() == 1
        assert enums.TaskEventKind.LOST in [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)]

        # The late heartbeat from the resumed worker.
        assert await backend.renew_agent_lease(agent_id, claim.epoch) is False

        agent = await Agent.objects.aget(pk=agent_id)
        assert agent.connected is False
        assert liveness.agent_is_live(agent.connected, agent.last_seen) is False

    async def test_reconnect_after_revocation_gets_a_fresh_epoch(self):
        # Revocation must not wedge the agent: a genuine reconnect still claims, without force.
        task = await build_task("revoke-reconnect")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        old = await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)
        await backend.reconcile_stale_agents()

        fresh = await backend.on_agent_connected(agent_id, "c2", session_id="S2", force=False)
        assert fresh.claimed and fresh.epoch > old.epoch
        assert await backend.renew_agent_lease(agent_id, fresh.epoch) is True


class TestClaimIsAtomic:
    async def test_live_incumbent_is_rejected_without_force(self):
        task = await build_task("claim-gate")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        rejected = await backend.on_agent_connected(agent_id, "c2", session_id="S2")

        assert rejected.claimed is False
        assert rejected.epoch is None
        assert rejected.tasks == []

    async def test_stale_incumbent_is_displaced_without_force(self):
        # Pinned behaviour (see test_conflict.test_stale_incumbent_reconnects_without_force):
        # a dead connection must never wedge the agent behind a ``--force`` reconnect.
        task = await build_task("claim-stale")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)

        claim = await backend.on_agent_connected(agent_id, "c2", session_id="S1", force=False)
        assert claim.claimed is True
        assert claim.displaced_incumbent is True

    async def test_concurrent_claims_yield_a_single_owner(self):
        # The TOCTOU: the gate used to read connected/last_seen off the instance loaded during
        # authentication and the write re-fetched afterwards, so two registrations racing on
        # different workers could both see the same stale incumbent, both pass, and both be
        # handed the in-flight work as Init inquiries. Gate + write now share one row lock.
        #
        # Driven through ``asyncio.to_thread`` rather than ``gather`` over the async wrapper:
        # ``database_sync_to_async`` is thread-sensitive and would serialize every call onto one
        # thread, so an ``asyncio.gather`` here races nothing and passes even against the
        # unlocked read-then-write it is supposed to catch. Real threads, real connections.
        task = await build_task("claim-race")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c0", session_id="S0")
        await _expire_lease(agent_id)

        workers = 6
        start = threading.Barrier(workers, timeout=30)

        def claim(i):
            from django.db import connection

            try:
                # Open this thread's connection BEFORE the barrier: connection setup costs
                # milliseconds and otherwise staggers the threads far enough apart that the
                # read→write window closes on its own and the race is never attempted.
                connection.ensure_connection()
                Agent.objects.filter(pk=agent_id).exists()
                start.wait()
                return backend._claim_lease_sync(agent_id, f"c{i}", "S0", False)
            finally:
                connection.close()

        results = await asyncio.gather(*[asyncio.to_thread(claim, i) for i in range(workers)])

        winners = [r for r in results if r[0]]
        assert len(winners) == 1, f"expected exactly one owner, got {len(winners)}"
        # And only the winner's epoch is the one now on the row.
        agent = await Agent.objects.aget(pk=agent_id)
        assert winners[0][1] == agent.lease_epoch


class TestSweepIsIdempotentAcrossWorkers:
    async def test_concurrent_sweeps_emit_one_terminal_event(self, settings):
        # Production runs several daphne processes, each with its own reaper loop, so the sweep
        # is genuinely concurrent. It used to select → save → reconcile with no claim, so every
        # worker that saw the stale row went on to emit its own terminal TaskEvent.
        _grace(settings, 0)
        task = await build_task("sweep-race", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)

        healed = await asyncio.gather(*[backend.reconcile_stale_agents() for _ in range(4)])

        assert sum(healed) == 1, f"expected exactly one worker to heal the agent, got {sum(healed)}"
        kinds = [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)]
        assert kinds.count(enums.TaskEventKind.LOST) == 1

    async def test_repeated_sweeps_do_not_pile_up_events(self, settings):
        # The re-entrancy guarantee the reaper depends on: it re-runs every stale window
        # forever, and a still-disconnected agent must not accrue an event per sweep.
        _grace(settings, 0)
        task = await build_task("sweep-reentrant", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(task.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await _expire_lease(agent_id)

        assert await backend.reconcile_stale_agents() == 1
        assert await backend.reconcile_stale_agents() == 0  # no longer stuck-connected
        await backend.reconcile_orphaned_executor_work(agent_id)  # the direct trigger, again

        kinds = [e.kind async for e in TaskEvent.objects.filter(task_id=task.pk)]
        assert kinds.count(enums.TaskEventKind.LOST) == 1
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.latest_event_kind == enums.TaskEventKind.LOST
