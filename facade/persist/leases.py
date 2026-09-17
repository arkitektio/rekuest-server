"""The executor lease: which connection is allowed to run an agent's work.

An agent is a singleton — exactly one live connection per agent row — and that is enforced here
rather than by trust. Every transition (claim on connect, release on a clean close, revoke by the
sweep) takes a row lock and goes through ``save()`` so the GraphQL agent feeds see it; the
heartbeat renewal is a lock-free compare-and-set whose rowcount *is* the answer to "am I still the
owner?". ``lease_epoch`` is the fencing token: bumping it makes a wedged connection's next renewal
match no row, so it closes itself instead of resurrecting an agent whose work was already failed.

See :mod:`facade.liveness` for the read predicate and why the write discipline lives here.
"""

import logging
from typing import Optional, Tuple

from channels.db import database_sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from facade import liveness, models, enums
from facade.probes.persist import probe_event_backend
from facade.deadlines import (
    grace_seconds,
)
from facade.ports import LeaseClaim

logger = logging.getLogger(__name__)


class AgentLeaseMixin:
    def _release_lease_sync(self, agent_id: int, connection_id: str | None) -> bool:
        """Release the lease on a clean close — only if it is still OURS. Returns whether we did.

        The generation guard and the write are one locked step. They used to be a read followed
        by an unconditional save: with several backends, the agent's reconnect can claim the lease
        on another process *between* the two, and the late save then marked the NEW, perfectly
        live holder ``connected=False``. Nothing ever repairs that — heartbeats deliberately do
        not re-assert ``connected`` — so the agent became unselectable and, a grace window later,
        its running work was failed.

        A displaced connection shutting down (``active_connection_id`` no longer ours) releases
        nothing and cascades nothing: the new owner is authoritative.
        """
        with transaction.atomic():
            agent = models.Agent.objects.select_for_update().get(id=agent_id)
            if connection_id is not None and agent.active_connection_id != connection_id:
                return False
            agent.connected = False
            agent.last_seen = timezone.now()
            agent.save(update_fields=["connected", "last_seen"])
        return True

    async def on_agent_disconnected(self, agent_id: int, connection_id: str | None = None) -> None:
        if not await database_sync_to_async(self._release_lease_sync)(agent_id, connection_id):
            return

        # Probes fail fast — hover-grade work is worthless once its executor is
        # gone, so no grace window applies to them (tasks keep theirs below).
        await probe_event_backend.fail_all_for_agent(agent_id)

        # Grace window: instead of failing in-flight work immediately, wait — a brief blip
        # that reconnects with the same session reclaims it. The window is not a timer: it
        # starts at the ``last_seen`` we just wrote, and ``reconcile_disconnected_agents``
        # (the reaper sweep, on whichever backend gets there first) fails the work once it has
        # elapsed and the agent is still gone. grace<=0 keeps the immediate, inline behaviour.
        if grace_seconds() <= 0:
            await self.reconcile_orphaned_executor_work(agent_id)

    def _revoke_lease_sync(self, agent_id: int) -> bool:
        """Revoke one stuck-connected agent's lease under a row lock. Returns whether we won.

        Re-checks staleness *under the lock*, so this doubles as the claim that makes the sweep
        multi-worker safe: whichever worker gets the lock first flips ``connected`` and the
        others then see a row that is no longer stale and back off, instead of every worker
        racing on to ``reconcile_orphaned_executor_work`` and emitting duplicate terminal
        events. A reconnect that landed between the scan and the lock also lands here.

        Bumping ``lease_epoch`` is the part a boolean cannot express: it *fences* the wedged
        connection, so if that worker's event loop later resumes, its heartbeat renewal
        compare-and-set matches no row and it closes itself instead of resurrecting an agent
        whose in-flight work has already been failed.

        Uses ``Model.save()`` (NOT ``.aupdate``) so ``agent_post_save`` fires and the GraphQL
        agent/``active`` subscriptions + dashboards refresh to reality. ``last_seen`` and
        ``active_connection_id`` are deliberately left untouched: ``last_seen`` is the true
        last-contact time the orphan cutoff depends on, and clearing ``active_connection_id``
        could let a still-wedged socket's later disconnect pass the generation guard.
        """
        with transaction.atomic():
            agent = models.Agent.objects.select_for_update().get(id=agent_id)
            if not liveness.agent_is_stale(agent.connected, agent.last_seen):
                return False  # healed or reconnected while we waited for the lock
            agent.connected = False
            agent.lease_epoch += 1
            agent.save(update_fields=["connected", "lease_epoch"])
        return True

    def _claim_lease_sync(self, agent_id: int, connection_id: str | None, session_id: str | None, force: bool) -> Tuple[bool, Optional[int], Optional[str], bool]:
        """Atomically decide the executor singleton and take the lease. Returns
        ``(claimed, epoch, prior_session, displaced_incumbent)``.

        The gate and the write happen under one ``select_for_update`` so they cannot be split:
        previously the gate read ``connected``/``last_seen`` off an instance loaded during
        authentication and the write re-fetched afterwards, so two concurrent registrations on
        different workers could both observe the same stale incumbent, both pass, and both be
        handed the in-flight work as ``Init`` inquiries.

        Gate: reject only a *provably live* incumbent (``connected`` AND a fresh heartbeat)
        when ``force`` is not set. A STALE incumbent — ``connected`` stuck True but the lease
        expired (crashed worker, lost in-memory timers) — is displaced without ``force``, so a
        dead connection never wedges the agent behind a ``--force`` reconnect.

        Bumping ``lease_epoch`` is what fences the previous owner: its next heartbeat renewal
        compare-and-sets against an epoch that no longer exists, matches no row, and it closes.
        """
        with transaction.atomic():
            agent = models.Agent.objects.select_for_update().get(id=agent_id)
            if liveness.agent_is_live(agent.connected, agent.last_seen) and not force:
                return False, None, agent.active_session_id, False

            prior_session = agent.active_session_id
            displaced_incumbent = agent.connected

            agent.lease_epoch += 1
            agent.connected = True
            agent.last_seen = timezone.now()
            agent.active_connection_id = connection_id
            agent.active_session_id = session_id
            agent.save(update_fields=["lease_epoch", "connected", "last_seen", "active_connection_id", "active_session_id"])

        return True, agent.lease_epoch, prior_session, displaced_incumbent

    async def on_agent_connected(self, agent_id: int, connection_id: str | None = None, session_id: str | None = None, force: bool = False) -> LeaseClaim:
        claimed, epoch, prior_session, displaced_incumbent = await database_sync_to_async(self._claim_lease_sync)(agent_id, connection_id, session_id, force)
        if not claimed:
            return LeaseClaim(claimed=False)

        # The agent is live again, so the grace window (``reconcile_disconnected_agents``) no
        # longer matches it — nothing to cancel. Work it never picked up is not "in flight":
        # its Assign is still queued for it (or the watchdog will redeliver it). Asking the agent
        # about it would have it answer "unknown → Critical" for a task it is about to receive,
        # and a fresh session would mark it DISCONNECTED just before it runs. Restart its pickup
        # clock instead, so a backlog that built up during the outage is not redelivered at once.
        await models.Task.objects.filter(
            agent_id=agent_id,
            is_done=False,
            picked_up_at__isnull=True,
            latest_event_kind=enums.TaskEventKind.QUEUED,
            dispatched_at__isnull=False,
        ).aupdate(dispatched_at=timezone.now())

        in_flight = [a async for a in models.Task.objects.select_related("implementation", "action").filter(agent_id=agent_id).filter(self._reclaimable_q())]

        # A different session means a FRESH process took over (the old one died): the prior
        # in-flight work is orphaned and must fail-and-cascade rather than be reclaimed.
        if prior_session is not None and session_id is not None and prior_session != session_id:
            await self._fail_and_cascade_inflight(in_flight)
            return LeaseClaim(claimed=True, epoch=epoch, tasks=[], displaced_incumbent=displaced_incumbent)

        # Same session (or first connect / no session info) → reclaim: hand the in-flight
        # work back as inquiries so the surviving process can re-sync.
        return LeaseClaim(claimed=True, epoch=epoch, tasks=in_flight, displaced_incumbent=displaced_incumbent)

    @staticmethod
    def _reclaimable_q() -> Q:
        """Open work a (re)connecting agent may actually hold — what it is inquired about.

        Same shape as :meth:`_orphanable_q` but WITHOUT its ``~DISCONNECTED`` term: a same-session
        reconnect is how a fate-unknown task gets its real outcome reported, so the agent must be
        asked about those, whereas a *sweep* has nothing left to do to them.

        Spelled out rather than derived from ``_orphanable_q`` on purpose. ``_orphanable_q() | Q(
        is_done=False, latest_event_kind=DISCONNECTED)`` reads equivalent and is not: the added
        disjunct would readmit DISCONNECTED *higher-order wrappers*, which both predicates exclude
        — and asking an agent about a virtual wrapper it never held gets "unknown task" back, which
        finalizes it wrongly.
        """
        return Q(is_done=False) & ~Q(latest_event_kind=enums.TaskEventKind.QUEUED, picked_up_at__isnull=True) & ~Q(implementation__higher_order_for__isnull=False)

    async def holds_lease(self, agent_id: int, lease_epoch: int) -> bool:
        """Whether ``lease_epoch`` is still the agent's current lease — asked before every delivery.

        The heartbeat renewal answers the same question, but only every
        ``AGENT_HEARTBEAT_INTERVAL``. The hint that tells a displaced connection to stop
        (``agent.displace``) travels over the channel layer, which drops messages when a
        process's receive queue is full; without this check the old connection kept popping the
        new holder's frames off the shared queue — and "delivering" them into a dead socket —
        until its next heartbeat. One primary-key lookup per frame; frames are rare.
        """
        return await models.Agent.objects.filter(pk=agent_id, lease_epoch=lease_epoch).aexists()

    async def renew_agent_lease(self, agent_id: int, lease_epoch: int) -> bool:
        """Renew the executor lease — the hot path, once per heartbeat per agent.

        A lock-free compare-and-set: the rowcount *is* the answer to "am I still the owner?".
        Returns False when this connection has been displaced by a newer registration or
        revoked by the stale sweep (either bumps ``lease_epoch``); the caller must then close,
        because a connection that cannot renew must not keep executing work.

        Deliberately ``.aupdate()`` rather than ``save()``: nothing observable transitions on a
        renewal, so this must NOT fire ``agent_post_save`` — that would broadcast an
        ``AgentChange`` to the whole organization every ``AGENT_HEARTBEAT_INTERVAL`` per agent.
        """
        rows = await models.Agent.objects.filter(id=agent_id, lease_epoch=lease_epoch).aupdate(last_seen=timezone.now())
        return rows == 1
