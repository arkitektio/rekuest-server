import logging
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from facade import inputs, liveness, models, enums, messages
from facade.probes.persist import probe_event_backend
from facade.grace import (
    disconnected_expiry_seconds,
    grace_seconds,
    pickup_deadline_seconds,
    progress_lease_seconds,
)
from facade.higher_order import project_returns
from facade.ports import LeaseClaim

logger = logging.getLogger(__name__)

# The pickup watchdog's budget: the original dispatch plus ONE redelivery. Failed handoffs
# (redis down, webhook 5xx) count — otherwise a permanently broken transport never fails.
MAX_DISPATCH_ATTEMPTS = 2

_TERMINAL_KINDS = (
    enums.TaskEventKind.COMPLETED,
    enums.TaskEventKind.CANCELLED,
    enums.TaskEventKind.INTERRUPTED,
    enums.TaskEventKind.FAILED,
    enums.TaskEventKind.CRITICAL,
)


class ModelPersistBackend:
    """The DB-truth backend (satisfies :class:`facade.ports.PersistBackend`).

    **Stateless by construction.** This object holds no state: every deadline it enforces
    starts at a DB column (``Agent.last_seen``, ``Task.dispatched_at`` / ``interrupt_at`` /
    ``last_progress_at``) and is acted on by a pure, idempotent ``reconcile_*`` / ``expire_*`` /
    ``escalate_*`` sweep, driven by :mod:`facade.reaper` inside every backend process. A backend
    can therefore die at any instant without losing a pending action, and any number of
    backends may sweep concurrently: each task/agent transition is a row-locked claim with
    exactly one winner, and the winner alone emits the ``TaskEvent``.

    Writes to an agent's liveness columns follow one rule (see :mod:`facade.liveness`):
    **transitions** (claim / release / revoke) take a row lock and go through ``save()`` so
    ``agent_post_save`` fires; **renewal** (the heartbeat, the only hot path) is a lock-free
    compare-and-set on ``lease_epoch`` whose rowcount is the answer.
    """

    async def _unfold_to_higher_order(
        self,
        child_task_id: str,
        kind,
        returns: Optional[dict] = None,
        message: Optional[str] = None,
        task: Optional[models.Task] = None,
    ) -> None:
        """If this task is the child of a higher-order wrapper, re-emit a mapped event on it.

        The lower implementation runs on a child task; the user watches the wrapper. So we
        project the child's returns back onto the wrapper's return ports and emit the corresponding
        event on the wrapper (linked via ``delegated_to``), which the subscription layer broadcasts.
        Non-higher-order children (hooks, dependency sub-assignments) are ignored.

        The overwhelmingly common case is an ordinary task, so the ``is_higher_order_child``
        flag gates the parent join: handlers that already hold the row pass ``task`` (zero
        extra queries); the fire-and-forget Yield path does one slim flag read instead of
        the two-table ``select_related`` it used to run on every yield.
        """
        if task is not None:
            if not task.is_higher_order_child:
                return
        else:
            try:
                is_higher_order_child = await models.Task.objects.values_list("is_higher_order_child", flat=True).aget(id=child_task_id)
            except models.Task.DoesNotExist:
                return
            if not is_higher_order_child:
                return

        try:
            child = await models.Task.objects.select_related("parent", "parent__implementation").aget(id=child_task_id)
        except models.Task.DoesNotExist:
            return

        parent = child.parent
        if parent is None:
            return
        parent_impl = parent.implementation
        if parent_impl is None or parent_impl.higher_order_for_id is None:
            return  # not a higher-order child

        config = parent_impl.higher_order_config or {}

        event: Dict[str, Any] = {"delegated_to": child}
        if kind == enums.TaskEventKind.YIELD:
            event["returns"] = project_returns(config, returns)
        if message is not None:
            event["message"] = message
        # Through the claim like every other transition: the wrapper's row is written by whoever
        # finalizes its child — an agent report on one backend, a sweep on another — and an
        # unlocked write here let two of them both finish the wrapper (two terminal events), or
        # let a late YIELD re-open one that was already done.
        await self._claim(parent.pk, to_kind=kind, mark_done=kind in _TERMINAL_KINDS, event=event)

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

    async def reconcile_orphaned_executor_work(self, agent_id: int) -> None:
        """Fail an agent's in-flight work after a confirmed loss. Pure, idempotent DB op.

        The authoritative reconcile shared by every trigger (inline on a zero-grace disconnect,
        the stale-agent sweep, the disconnected-agent sweep). No-op if the agent is live again —
        a reconnect in the meantime means the work is being reclaimed, not orphaned.

        The bail-out asks :func:`liveness.agent_is_live`, not ``agent.connected``. Every other
        liveness decision goes through that one predicate, and this used to be the exception:
        a stuck-connected agent whose lease had expired would make this a silent no-op, so its
        work stayed ``is_done=False`` forever. Today the sweep happens to revoke (flipping
        ``connected``) *before* calling here, which masks it — but that is sequencing luck, not
        a guarantee, and it breaks the moment a new trigger calls this directly.
        """
        agent = await models.Agent.objects.aget(id=agent_id)
        if liveness.agent_is_live(agent.connected, agent.last_seen):
            return
        in_flight = [a async for a in models.Task.objects.select_related("implementation", "action").filter(agent_id=agent_id).filter(self._orphanable_q())]
        await self._fail_and_cascade_inflight(in_flight)

    @staticmethod
    def _orphanable_q() -> Q:
        """In-flight work an executor's death actually orphans.

        Excluded, because there is nothing (more) to do for them here:

        * already ``DISCONNECTED`` — handled; re-scanning them every sweep would cost a row
          lock per task per tick until they expire;
        * ``QUEUED`` and never picked up — the agent never had them. Their Assign is still in
          the agent's redis queue (or will be redelivered by the pickup watchdog), so they
          simply run when the agent is back; ``expire_disconnected_tasks`` bounds the wait;
        * higher-order wrappers — virtual, never dispatched; their fate is their child's,
          projected by ``_unfold_to_higher_order``.
        """
        return (
            Q(is_done=False)
            & ~Q(latest_event_kind=enums.TaskEventKind.DISCONNECTED)
            & ~Q(latest_event_kind=enums.TaskEventKind.QUEUED, picked_up_at__isnull=True)
            & ~Q(implementation__higher_order_for__isnull=False)
        )

    async def reconcile_disconnected_agents(self) -> int:
        """Fail the work of websocket agents that disconnected and stayed gone past the grace.

        This IS the grace window. A clean disconnect only records ``connected=False`` +
        ``last_seen``; whichever backend's sweep first sees that timestamp age past the grace
        fails the work — so the window survives a backend restart and needs no owner. Webhook
        agents never set ``connected``/``last_seen`` (no socket) and are not swept here.
        Returns the number of agents reconciled.
        """
        cutoff = timezone.now() - timedelta(seconds=grace_seconds())
        agent_ids = [
            agent_id
            async for agent_id in models.Task.objects.filter(self._orphanable_q())
            .filter(agent__kind=enums.AgentKind.WEBSOCKET.value, agent__connected=False)
            .filter(Q(agent__last_seen__lt=cutoff) | Q(agent__last_seen__isnull=True))
            .values_list("agent_id", flat=True)
            .distinct()
        ]
        for agent_id in agent_ids:
            await self.reconcile_orphaned_executor_work(agent_id)
        return len(agent_ids)

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

    async def reconcile_stale_agents(self) -> int:
        """Heal websocket agents whose ``connected`` is stuck True past the stale window.

        The disconnect handler only runs on a clean socket close; a crashed/killed worker leaves
        ``connected=True`` with a stale ``last_seen`` forever, and the in-memory grace timers die
        with the process. This is the DB-authoritative safety net: revoke the lease
        (``connected=False`` + epoch bump) and reconcile the orphaned in-flight work.

        Idempotent and multi-worker-safe: the revoke is a lock-guarded claim, so only the worker
        that actually flips a row goes on to reconcile it. Driven by the in-process reaper loop
        (:mod:`facade.reaper`) of every backend. Returns the number healed.
        """
        stale = [
            a
            async for a in models.Agent.objects.select_related("organization")
            .filter(kind=enums.AgentKind.WEBSOCKET.value)
            .filter(liveness.stale_agent_q(prefix=""))
        ]
        healed = 0
        for agent in stale:
            if not await database_sync_to_async(self._revoke_lease_sync)(agent.pk):
                continue  # another worker's sweep (or a reconnect) got there first
            await probe_event_backend.fail_all_for_agent(agent.pk)  # calls fail fast, no grace
            await self.reconcile_orphaned_executor_work(agent.pk)  # now matches connected=False
            healed += 1
        return healed

    def _build_redispatch_assign_sync(self, task_id: int) -> "messages.Assign | None":
        """Rebuild the Assign message for an idempotent task's re-dispatch, or None.

        Sync (run via ``database_sync_to_async``): token minting walks lazy FK chains.
        Returns None when the task lacks the identity needed to re-mint (no caller/
        implementation) or when a strict provenance policy refuses — the caller then falls
        back to the DISCONNECTED fate-unknown path.
        """
        from facade.caller_context import CallerContext
        from facade.provenance import mint_token_for_task

        task = models.Task.objects.select_related(
            "agent", "implementation", "action", "caller__user", "caller__client", "caller__organization"
        ).get(pk=task_id)

        if task.implementation is None or task.caller is None or task.caller.user is None or task.caller.organization is None:
            return None

        ctx = CallerContext(user=task.caller.user, client=task.caller.client, organization=task.caller.organization, roles=[])
        try:
            token = mint_token_for_task(task, ctx)
        except ValueError:
            return None

        return messages.Assign(
            task=str(task.pk),
            args=task.args or {},
            user=str(task.caller.user.sub),
            org=str(task.caller.organization.slug),
            reference=str(task.reference) if task.reference is not None else None,
            capture=task.capture,
            step=task.step or None,
            resolution=str(task.resolution_id) if task.resolution_id else None,
            interface=task.implementation.interface,
            action=str(task.action.hash),
            implementation=str(task.implementation_id),
            parent=str(task.parent_id) if task.parent_id else None,
            root=str(task.root_id) if task.root_id else None,
            token=token,
        )

    def _claim_task_transition_sync(
        self,
        task_id: int,
        *,
        to_kind: str,
        mark_done: bool = False,
        skip_if_kind: str | None = None,
        only_if: Callable[[models.Task], bool] | None = None,
        extra: Dict[str, Any] | None = None,
        event: Dict[str, Any] | None = None,
        skip_locked: bool = False,
    ) -> bool:
        """Take a task transition under a row lock. Returns whether we won.

        THE primitive behind every server-decided transition and every agent-reported
        terminal. Sweeps are re-entrant *and* run concurrently in every backend process, and an
        agent retries an unacked terminal report over whatever connection it has next — so
        "is this task still open?" must be answered and acted on atomically. Reading the flag
        and then writing it lets two backends both observe an open task and both emit a terminal
        ``TaskEvent`` for it. Losing the claim (already done, already in ``skip_if_kind``, or
        ``only_if`` no longer holds under the lock) means somebody else handled it; skip silently.

        ``event`` (extra ``TaskEvent`` fields; ``{}`` for a bare one) writes the event in the SAME
        transaction as the transition, so a crash cannot leave a finished task without its
        terminal event. Fan-out happens on commit (``facade.signals``). ``extra`` sets further
        columns. ``skip_locked`` makes a sweep step over a row another backend is working on
        instead of queueing behind it.

        ``save()`` rather than ``.update()``: a task transition is observable, and
        ``task_post_save`` fans it out to the agent/child task feeds.
        """
        with transaction.atomic():
            task = models.Task.objects.select_for_update(skip_locked=skip_locked, of=("self",)).filter(pk=task_id).first()
            if task is None:
                return False  # deleted, or (skip_locked) being handled by another backend
            if task.is_done or (skip_if_kind is not None and task.latest_event_kind == skip_if_kind):
                return False
            if only_if is not None and not only_if(task):
                return False
            task.latest_event_kind = to_kind
            update_fields = ["latest_event_kind"]
            if mark_done:
                task.is_done = True
                task.finished_at = timezone.now()
                update_fields += ["is_done", "finished_at"]
            for field, value in (extra or {}).items():
                setattr(task, field, value)
                update_fields.append(field)
            task.save(update_fields=update_fields)
            if event is not None:
                models.TaskEvent.objects.create(task=task, kind=to_kind, **event)
        return True

    async def _claim(self, task_id: int, **kwargs: Any) -> bool:
        """Async face of :meth:`_claim_task_transition_sync`."""
        return await database_sync_to_async(self._claim_task_transition_sync)(task_id, **kwargs)

    @staticmethod
    def _effect_of(task: models.Task) -> str:
        """The task's effect class — decides the retry axis (physical work is never re-run)."""
        implementation = task.implementation
        return implementation.effect if implementation is not None else enums.EffectClassChoices.NONE.value

    async def _fail_and_cascade_inflight(self, tasks: List[models.Task]) -> None:
        """Mark orphaned in-flight work along the retry axis.

        ``effect:physical`` failed ambiguously (the executor vanished) → CRITICAL (terminal,
        never retried). Idempotent actions → QUEUED + the Assign re-broadcast into the
        agent's redis queue (which retains messages for offline agents), so the work re-runs
        on reconnect — a same-session reclaim after grace expiry may double-execute, which is
        safe by the idempotent contract. Everything else → DISCONNECTED (fate unknown,
        recoverable until ``expire_disconnected_tasks`` finalizes it).

        Work the agent never picked up is left alone entirely (see :meth:`_orphanable_q`): it
        is not orphaned, merely undelivered. That is also what makes this re-entrant — a
        re-queued idempotent task is exactly such a row, so a later pass neither piles a second
        Assign into the queue nor degrades it to DISCONNECTED.

        Every branch claims the transition first and emits its ``TaskEvent`` inside the claim,
        so concurrent sweeps produce exactly one event per task rather than one each.
        """
        for task in tasks:
            if task.latest_event_kind == enums.TaskEventKind.QUEUED and task.picked_up_at is None:
                continue  # undelivered, not orphaned (callers that pre-filter never get here)

            if self._effect_of(task) == enums.EffectClassChoices.PHYSICAL.value:
                message = "Executor lost while running physical-effect work — terminal, not retried."
                if await self._claim(task.pk, to_kind=enums.TaskEventKind.CRITICAL, mark_done=True, event={"message": message}):
                    await self._unfold_to_higher_order(str(task.pk), enums.TaskEventKind.CRITICAL, message=message, task=task)
                continue

            if task.action is not None and task.action.idempotent:
                # Built BEFORE the claim: if there is no re-dispatchable identity we must fall
                # through to the fate-unknown branch, not leave the task marked QUEUED.
                assign_message = await database_sync_to_async(self._build_redispatch_assign_sync)(task.pk)
                if assign_message is not None:
                    # Back to "dispatched, not picked up": the pickup watchdog takes it from here
                    # once the agent is live again (``on_agent_connected`` restarts the clock).
                    won = await self._claim(
                        task.pk,
                        to_kind=enums.TaskEventKind.QUEUED,
                        skip_if_kind=enums.TaskEventKind.QUEUED,
                        extra={"picked_up_at": None, "dispatched_at": timezone.now(), "dispatch_attempts": 1},
                        event={"message": "Executor lost — idempotent action re-queued for redelivery."},
                    )
                    if won:
                        await self._dispatch(task.pk, task.agent_id, assign_message)
                    continue
                # No re-dispatchable identity → fall through to fate-unknown.

            await self._claim(
                task.pk,
                to_kind=enums.TaskEventKind.DISCONNECTED,
                skip_if_kind=enums.TaskEventKind.DISCONNECTED,
                event={"message": "Agent disconnected. Fate unknown"},
            )

    async def _dispatch(self, task_id: int, agent_id: int, assign_message: "messages.Assign") -> bool:
        """Hand an Assign to the agent's transport; on failure record that it never left.

        ``dispatched_at`` means "successfully handed over at". A failed handoff resets it to
        NULL (best effort) so the pickup watchdog retries it, and — because NULL proves the agent
        cannot have the task — may do so even for physical-effect work.
        """
        from facade.consumers.async_consumer import AgentConsumer  # lazy: avoids import cycle

        try:
            delivered = await sync_to_async(AgentConsumer.broadcast)(agent_id, assign_message)
        except Exception:
            logger.error("Dispatching task %s to agent %s failed", task_id, agent_id, exc_info=True)
            delivered = False
        if delivered is False:
            await models.Task.objects.filter(pk=task_id, is_done=False).aupdate(dispatched_at=None)
            return False
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

        Unlike :meth:`_orphanable_q` this keeps ``DISCONNECTED`` rows: a same-session reconnect
        is exactly how a fate-unknown task gets its real outcome reported.
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

    async def is_task_open(self, task_id: str) -> bool:
        """Whether an Assign for this task is still worth delivering (the delivery-time fence).

        An Assign can outlive its task in the agent's queue: the server may finalize the task
        (pickup watchdog, expiry, a cancel of undelivered work) while the frame still waits for
        an offline agent. Deliberately conservative: only a task that verifiably IS finalized
        closes the fence — an id we cannot resolve is delivered, never silently swallowed.
        """
        try:
            return not await models.Task.objects.filter(pk=task_id, is_done=True).aexists()
        except (ValueError, TypeError):
            return True

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

    async def get_or_create_caller_id(self, agent_id: int) -> str:
        """The durable ``Caller`` id for an agent's identity (user/client/organization).

        A connection joins ``task_caller_{caller_id}`` to receive the events of work it
        originated. Mirrors ``get_caller_for_context`` (``facade/backend.py``) but resolves
        the identity from the agent instead of a GraphQL request.
        """
        agent = await models.Agent.objects.select_related("user", "client", "organization").aget(id=agent_id)
        caller, _ = await models.Caller.objects.aget_or_create(
            client=agent.client,
            user=agent.user,
            organization=agent.organization,
        )
        return str(caller.pk)

    async def on_caller_assign(
        self,
        agent_id: int,
        message: messages.AssignRequest,
        connection_id: str | None = None,
        session_id: str | None = None,
    ) -> Tuple[models.Task, bool]:
        """Assign *dependent* work requested by an agent over the socket.

        Idempotent on ``(caller, reference)`` and durable-before-return: a resend of the same
        ``reference`` returns the existing task with ``created=False`` rather than creating a
        duplicate. Raises ``PermissionError`` for a parentless (root) assign — roots must trace
        to an accountable human, so they originate solely from the GraphQL ``assign`` mutation
        (see the human-root invariant in ``facade.provenance``). Runs the sync postman backend
        off the event loop.
        """
        return await database_sync_to_async(self._caller_assign_sync)(agent_id, message, connection_id, session_id)

    def _caller_assign_sync(
        self,
        agent_id: int,
        message: messages.AssignRequest,
        connection_id: str | None = None,
        session_id: str | None = None,
    ) -> Tuple[models.Task, bool]:
        # Imported lazily: facade.backend → async_consumer → agent_protocol → persist_backend
        # would otherwise be a circular import at module load.
        from facade.backend import controll_backend
        from facade.caller_context import CallerContext
        from facade.provenance import principal

        agent = models.Agent.objects.select_related("user", "client", "organization").get(id=agent_id)
        caller, _ = models.Caller.objects.get_or_create(client=agent.client, user=agent.user, organization=agent.organization)

        # Idempotency: a resend of the same reference returns the existing task.
        existing = models.Task.objects.filter(caller=caller, reference=message.reference).first()
        if existing is not None:
            return existing, False

        if message.parent is None:
            raise PermissionError("An agent may only assign dependent work: 'parent' is required. Root tasks originate from the GraphQL assign mutation, where the initiator is an accountable human.")

        ctx = CallerContext.from_agent(agent, roles=principal.roles_for_caller(caller))
        hooks = [inputs.HookInputModel(**h) for h in message.hooks] if message.hooks else None
        assign_input = inputs.AssignInputModel(
            reference=message.reference,
            args=message.args,
            action=message.action,
            action_hash=message.action_hash,
            implementation=message.implementation,
            agent=message.agent,
            interface=message.interface,
            parent=message.parent,
            dependency=message.dependency,
            method=message.method,
            resolution=message.resolution,
            hooks=hooks,
            capture=message.capture,
            step=message.step,
        )
        # A dependent task's fate follows its parent, so nothing about this connection needs
        # recording on the row: if this agent dies, the executor-death cascade covers its work,
        # and if the parent's tree is cancelled the child goes with it.
        # ``created`` is the backend's verdict, not an assumption: a resend racing the original on
        # another backend loses the unique constraint and must report ``created=False``.
        return controll_backend.assign_with_status(ctx, assign_input)

    def _caller_control_sync(self, agent_id: int, task_id: str, op: str, *, step: bool = False) -> models.Task:
        """Ownership-check then dispatch a control op on the sync postman backend.

        A caller may only control tasks whose ``caller`` is its own identity. Raises
        ``Task.DoesNotExist`` (unknown), ``PermissionError`` (not the caller), or
        ``ValueError`` (already terminal — from the postman backend).
        """
        from facade import inputs
        from facade.backend import controll_backend

        agent = models.Agent.objects.select_related("user", "client", "organization").get(id=agent_id)
        caller, _ = models.Caller.objects.get_or_create(client=agent.client, user=agent.user, organization=agent.organization)
        task = models.Task.objects.get(id=task_id)
        if task.caller_id != caller.pk:
            raise PermissionError("Not authorized to control this task (not its caller).")

        ref = str(task_id)
        ops = {
            "cancel": lambda: controll_backend.cancel(inputs.CancelInputModel(task=ref), caller=caller),
            "interrupt": lambda: controll_backend.interrupt(inputs.InterruptInputModel(task=ref), caller=caller),
            "pause": lambda: controll_backend.pause(inputs.PauseInputModel(task=ref), caller=caller),
            "resume": lambda: controll_backend.resume(inputs.ResumeInputModel(task=ref, step=step), caller=caller),
        }
        return ops[op]()

    async def on_caller_cancel(self, agent_id: int, message: messages.CancelRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        task = await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "cancel")
        if message.auto_interrupt is not None:
            # The escalation deadline is a column, not a timer: ``escalate_due_controls`` (the
            # reaper sweep, on any backend) fires it. Wins over the global control deadline.
            await models.Task.objects.filter(pk=task.pk, is_done=False).aupdate(interrupt_at=timezone.now() + timedelta(seconds=float(message.auto_interrupt)))
        return task

    async def on_caller_interrupt(self, agent_id: int, message: messages.InterruptRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "interrupt")

    async def on_caller_pause(self, agent_id: int, message: messages.PauseRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "pause")

    async def on_caller_resume(self, agent_id: int, message: messages.ResumeRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "resume", step=message.step)

    async def _escalate_to_interrupt(self, task_id: str | int) -> None:
        """A cancel's deadline passed unconfirmed: escalate it to an interrupt. Idempotent."""
        from facade import inputs
        from facade.backend import controll_backend

        def _do() -> None:
            task = models.Task.objects.get(id=task_id)
            if task.is_done:
                return  # the cancel confirmed (or otherwise terminal) before the window — no-op
            controll_backend.interrupt(inputs.InterruptInputModel(task=str(task_id)))

        try:
            await database_sync_to_async(_do)()
        except models.Task.DoesNotExist:
            return

    async def escalate_due_controls(self, limit: int = 200) -> int:
        """Act on control deadlines (``Task.interrupt_at``) that have passed. Returns the count.

        * an unconfirmed **cancel** → escalated to an interrupt (``auto_interrupt`` on the socket
          ``CancelRequest``, or the global control deadline);
        * an unconfirmed **interrupt** (only ever armed by the global control deadline) →
          finalized as INTERRUPTED: the server stops waiting for an executor that will not answer.

        The claim is a compare-and-set on the deadline itself — whichever backend clears
        ``interrupt_at`` owns the escalation, so N backends sweeping produce one interrupt.
        """
        now = timezone.now()
        due = [row async for row in models.Task.objects.filter(is_done=False, interrupt_at__lt=now).values_list("pk", "interrupt_at", "latest_instruct_kind")[:limit]]
        handled = 0
        for task_id, deadline, instruct_kind in due:
            if instruct_kind == enums.TaskInstructKind.INTERRUPT:
                message = "Interrupt was never confirmed by the agent — finalized by the server."
                won = await self._claim(
                    task_id,
                    to_kind=enums.TaskEventKind.INTERRUPTED,
                    mark_done=True,
                    only_if=lambda t, deadline=deadline: t.interrupt_at == deadline,
                    extra={"interrupt_at": None},
                    event={"message": message},
                    skip_locked=True,
                )
                if won:
                    await self._unfold_to_higher_order(str(task_id), enums.TaskEventKind.INTERRUPTED, message=message)
                    handled += 1
                continue

            if not await models.Task.objects.filter(pk=task_id, is_done=False, interrupt_at=deadline).aupdate(interrupt_at=None):
                continue  # another backend took it, or the task went terminal meanwhile
            handled += 1
            if instruct_kind == enums.TaskInstructKind.CANCEL:
                try:
                    await self._escalate_to_interrupt(task_id)
                except Exception:
                    logger.error("Escalating task %s to an interrupt failed", task_id, exc_info=True)
            # Any other instruct (a resume after the cancel, …) superseded the deadline: dropped.
        return handled

    # ----------------------------------------------------------------------- #
    # Lifecycle confirmation handlers (the second phase)
    # ----------------------------------------------------------------------- #
    async def _agent_task(self, agent_id: int, task_id: str, *, reopen: bool = True) -> models.Task | None:
        """Fetch a task, asserting it belongs to the reporting agent — and note the pickup.

        The agent's *socket* is authenticated, but the task id inside the frame is not — it is
        whatever the agent put there. Without the ``agent_id`` predicate any authenticated agent
        could terminate, fail, or inject results into any task in any organization simply by
        naming its id. ``Task.agent`` is non-nullable and set at dispatch, so this is total.

        Every agent report passes through here, which makes it the one place that can answer
        "did the agent ever pick this up?": the first report stamps ``picked_up_at`` (a guarded
        UPDATE — no signal, and no extra query on any later report). ``latest_event_kind`` cannot
        answer it, because Progress/Log/Yield never move it. A non-terminal report on a
        ``DISCONNECTED`` task (``reopen``) proves the work is alive after all, so it is claimed
        back to STARTED before the expiry sweep could finalize it.

        Returns ``None`` when the task is unknown *or* not this agent's, which callers treat the
        same way: drop the frame rather than tear down the transport.
        """
        try:
            x = await models.Task.objects.aget(id=task_id, agent_id=agent_id)
        except (models.Task.DoesNotExist, ValueError, TypeError):
            logging.warning(f"Agent {agent_id} reported on task {task_id}, which is not assigned to it. Dropping.")
            return None

        if x.picked_up_at is None and not x.is_done:
            now = timezone.now()
            await models.Task.objects.filter(pk=x.pk, picked_up_at__isnull=True).aupdate(picked_up_at=now)
            x.picked_up_at = now

        if reopen and not x.is_done and x.latest_event_kind == enums.TaskEventKind.DISCONNECTED:
            reopened = await self._claim(
                x.pk,
                to_kind=enums.TaskEventKind.STARTED,
                only_if=lambda t: t.latest_event_kind == enums.TaskEventKind.DISCONNECTED,
                event={"message": "The agent reported on this task again — reclaimed after the disconnect."},
            )
            if reopened:
                x.latest_event_kind = enums.TaskEventKind.STARTED
        return x

    async def _finalize_from_agent(self, agent_id: int, task_id: str, kind, *, message: str | None = None) -> None:
        """Persist an agent-reported terminal — exactly once, however often it is reported.

        The agent retries a terminal report until it sees an ``EventAck``, possibly over a new
        connection to a different backend while the first is still processing it. So the
        "already done?" check and the write are one row-locked claim, and the ``TaskEvent`` is
        written inside it: two backends receiving the same report produce one terminal event.
        """
        x = await self._agent_task(agent_id, task_id, reopen=False)
        if x is None:
            return
        if x.is_done:
            return  # dedup: a resent terminal report (the agent retries until EventAck)
        event = {"message": message} if message is not None else {}
        if not await self._claim(x.pk, to_kind=kind, mark_done=True, event=event):
            return  # lost the race to another report / a sweep — theirs is the outcome
        await self._unfold_to_higher_order(task_id, kind, message=message, task=x)

    async def on_agent_interrupted(self, agent_id: int, message: messages.Interrupted) -> None:
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.INTERRUPTED)

    async def _on_nonterminal_confirm(self, agent_id: int, task_id: str, kind, *, extra: Dict[str, Any] | None = None) -> None:
        """Persist a non-terminal lifecycle confirmation (started/paused/resumed)."""
        # A confirmation for an unknown task — or another agent's task — must not tear down the
        # transport; it is dropped.
        x = await self._agent_task(agent_id, task_id)
        if x is None:
            return
        if x.is_done:
            return
        await self._claim(x.pk, to_kind=kind, extra=extra, event={})

    async def on_agent_paused(self, agent_id: int, message: messages.Paused) -> None:
        # A suspended op stops reporting progress — don't let the silent-physical-op lease reap
        # it: clearing the stamp takes it out of ``reconcile_silent_physical_ops`` until the
        # next Progress re-arms it.
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.PAUSED, extra={"last_progress_at": None})

    async def on_agent_resumed(self, agent_id: int, message: messages.Resumed) -> None:
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.RESUMED)

    async def on_agent_started(self, agent_id: int, message: messages.Started) -> None:
        # The agent accepted and began executing — record it (mirrored to the caller as StartedEvent).
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.STARTED)

    async def on_agent_log(self, agent_id: int, message: messages.Log) -> None:
        logging.info(f"Log Task {message}")

        if await self._agent_task(agent_id, message.task) is None:
            return
        await models.TaskEvent.objects.acreate(
            task_id=message.task,
            kind=enums.TaskEventKind.LOG,
            message=message.message,
            level=message.level,
        )

    async def on_agent_yield(self, agent_id: int, message: messages.Yield) -> None:
        logging.info(f"Yield Task {message}")

        if await self._agent_task(agent_id, message.task) is None:
            return
        await models.TaskEvent.objects.acreate(
            task_id=message.task,
            kind=enums.TaskEventKind.YIELD,
            returns=message.returns,
        )
        await self._unfold_to_higher_order(message.task, enums.TaskEventKind.YIELD, returns=message.returns)

    async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None:
        logging.info(f"Completed Task {message}")
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.COMPLETED)

    async def on_agent_cancelled(self, agent_id: int, message: messages.Cancelled) -> None:
        logging.info(f"Cancelled Task {message}")
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.CANCELLED)

    async def on_agent_error(self, agent_id: int, message: messages.Failed) -> None:
        logging.info(f"Failed Task {message}")
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.FAILED, message=message.error)

    async def on_agent_critical(self, agent_id: int, message: messages.Critical) -> None:
        logging.info(f"Critical Task {message}")
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.CRITICAL, message=message.error)

    async def on_agent_progress(self, agent_id: int, message: messages.Progress) -> None:
        logging.info(f"Progress Task {message}")

        if await self._agent_task(agent_id, message.task) is None:
            return
        await models.TaskEvent.objects.acreate(
            task_id=message.task,
            kind=enums.TaskEventKind.PROGRESS,
            progress=message.progress,
            message=message.message,
        )
        await self._arm_progress_lease(message.task)

    async def _arm_progress_lease(self, task_id: str) -> None:
        """(Re)arm the silent-physical-op lease for a physical task, if enabled.

        Arming is stamping ``last_progress_at``; ``reconcile_silent_physical_ops`` is what
        fires. One guarded UPDATE — it matches nothing unless the task is open and physical.
        """
        if progress_lease_seconds() <= 0:
            return  # disabled — zero overhead on the progress hot-path
        await models.Task.objects.filter(
            id=task_id,
            is_done=False,
            implementation__effect=enums.EffectClassChoices.PHYSICAL.value,
        ).aupdate(last_progress_at=timezone.now())

    async def reconcile_silent_physical_op(self, task_id: str | int, *, cutoff=None) -> bool:
        """Fail a physical task that reported progress then went silent. Claim-based DB op."""
        message = "Physical op went silent past its progress lease — terminal, not retried."
        won = await self._claim(
            int(task_id),
            to_kind=enums.TaskEventKind.CRITICAL,
            mark_done=True,
            # Re-checked under the lock: a Progress that landed since the scan re-armed the lease.
            only_if=(lambda t: t.last_progress_at is not None and t.last_progress_at < cutoff) if cutoff is not None else None,
            event={"message": message},
            skip_locked=cutoff is not None,
        )
        if won:
            await self._unfold_to_higher_order(str(task_id), enums.TaskEventKind.CRITICAL, message=message)
        return won

    async def reconcile_silent_physical_ops(self, limit: int = 200) -> int:
        """Fail physical tasks whose last Progress is older than the progress lease."""
        lease = progress_lease_seconds()
        if lease <= 0:
            return 0
        cutoff = timezone.now() - timedelta(seconds=lease)
        silent = [
            pk
            async for pk in models.Task.objects.filter(
                is_done=False,
                last_progress_at__lt=cutoff,
                implementation__effect=enums.EffectClassChoices.PHYSICAL.value,
            ).values_list("pk", flat=True)[:limit]
        ]
        failed = 0
        for pk in silent:
            if await self.reconcile_silent_physical_op(pk, cutoff=cutoff):
                failed += 1
        return failed

    # ----------------------------------------------------------------------- #
    # Pickup watchdog + expiry (the "never stays QUEUED forever" sweeps)
    # ----------------------------------------------------------------------- #

    def _decide_unpicked_sync(self, task_id: int, cutoff) -> Tuple[str, "messages.Assign | None", int | None]:
        """Decide — under the row lock — what happens to one task nobody picked up.

        Returns ``(outcome, assign_message, agent_id)`` where outcome is ``"skip"`` (no longer a
        candidate / another backend has it), a terminal ``TaskEventKind`` (finalized here), or
        ``"redeliver"`` (stamped; the caller pushes ``assign_message`` AFTER this commit, so a
        push can never be observed for a row that then rolls back).
        """
        with transaction.atomic():
            task = models.Task.objects.select_for_update(skip_locked=True, of=("self",)).filter(pk=task_id).select_related("implementation").first()
            if task is None or task.is_done or task.picked_up_at is not None or task.latest_event_kind != enums.TaskEventKind.QUEUED:
                return "skip", None, None
            if (task.dispatched_at or task.created_at) >= cutoff:
                return "skip", None, None  # re-dispatched / clock restarted since the scan

            def finalize(kind, message: str) -> Tuple[str, None, None]:
                task.latest_event_kind = kind
                task.is_done = True
                task.finished_at = timezone.now()
                task.save(update_fields=["latest_event_kind", "is_done", "finished_at"])
                models.TaskEvent.objects.create(task=task, kind=kind, message=message)
                return kind, None, None

            # A control op already targeted it: the agent never had the task, so there is
            # nothing to wind down — honour the request instead of redelivering the Assign.
            if task.latest_instruct_kind == enums.TaskInstructKind.CANCEL:
                return finalize(enums.TaskEventKind.CANCELLED, "Cancelled before any agent picked the task up.")
            if task.latest_instruct_kind == enums.TaskInstructKind.INTERRUPT:
                return finalize(enums.TaskEventKind.INTERRUPTED, "Interrupted before any agent picked the task up.")

            if task.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
                return finalize(enums.TaskEventKind.CRITICAL, "Never picked up: the agent did not report on this task after it was redelivered.")

            # ``dispatched_at`` set = the Assign verifiably left for the agent. It may have been
            # received and be running with its reports lost — physical work is never sent twice.
            if task.dispatched_at is not None and self._effect_of(task) == enums.EffectClassChoices.PHYSICAL.value:
                return finalize(enums.TaskEventKind.CRITICAL, "Never picked up: physical-effect work is not redelivered.")

            assign_message = self._build_redispatch_assign_sync(task.pk)
            if assign_message is None:
                return finalize(enums.TaskEventKind.CRITICAL, "Never picked up, and the Assign could not be rebuilt for redelivery.")

            task.dispatched_at = timezone.now()
            task.dispatch_attempts += 1
            task.save(update_fields=["dispatched_at", "dispatch_attempts"])
            models.TaskEvent.objects.create(task=task, kind=enums.TaskEventKind.QUEUED, message="No report from the agent within the pickup deadline — Assign redelivered.")
            return "redeliver", assign_message, task.agent_id

    async def reconcile_unpicked_tasks(self, limit: int = 200) -> int:
        """The pickup watchdog: no task waits forever for an agent that looks alive.

        Every other safety net keys on the agent looking *dead*. This one covers the rest: the
        Assign was lost (a dead drain loop, a displaced connection that swallowed the frame, a
        push that failed after the row committed, a webhook that was down) or the agent dropped
        it without a word. A dispatched task whose **live** agent (or webhook endpoint) has
        reported nothing at all within ``PICKUP_DEADLINE`` is redelivered once; silent again →
        CRITICAL. Tasks of agents that are *not* live are left to the disconnect path.

        Keyed on ``picked_up_at`` (any report), never on ``latest_event_kind`` alone: a healthy
        task that only ever sends Progress/Yield still reads QUEUED. Virtual higher-order
        wrappers are never dispatched and are excluded. Returns the number acted on.
        """
        deadline = pickup_deadline_seconds()
        if deadline <= 0:
            return 0
        cutoff = timezone.now() - timedelta(seconds=deadline)
        candidates = [
            pk
            async for pk in models.Task.objects.filter(is_done=False, picked_up_at__isnull=True, latest_event_kind=enums.TaskEventKind.QUEUED)
            .filter(Q(dispatched_at__lt=cutoff) | Q(dispatched_at__isnull=True, created_at__lt=cutoff))
            .filter(Q(agent__kind=enums.AgentKind.WEBHOOK.value) | liveness.live_agent_q("agent"))
            .exclude(implementation__higher_order_for__isnull=False)
            .order_by("created_at")
            .values_list("pk", flat=True)[:limit]
        ]
        acted = 0
        for pk in candidates:
            outcome, assign_message, agent_id = await database_sync_to_async(self._decide_unpicked_sync)(pk, cutoff)
            if outcome == "skip":
                continue
            acted += 1
            if outcome == "redeliver":
                assert assign_message is not None and agent_id is not None
                await self._dispatch(pk, agent_id, assign_message)
            else:
                await self._unfold_to_higher_order(str(pk), outcome)
        return acted

    async def expire_disconnected_tasks(self, limit: int = 200) -> int:
        """Finalize work whose fate stayed unknown past ``DISCONNECTED_EXPIRY``.

        Two kinds of row wait on an agent that may never return: ``DISCONNECTED`` tasks
        (recoverable — a returning agent may still report the real outcome) and undelivered
        ``QUEUED`` tasks of an agent that is no longer live. Both stay open for the expiry
        window, then become CRITICAL so no caller waits forever. 0 disables. Returns the count.
        """
        expiry = disconnected_expiry_seconds()
        if expiry <= 0:
            return 0
        cutoff = timezone.now() - timedelta(seconds=expiry)
        expired = 0

        disconnected = [
            pk
            async for pk in models.Task.objects.filter(is_done=False, latest_event_kind=enums.TaskEventKind.DISCONNECTED)
            .annotate(last_event_at=Max("events__created_at"))
            .filter(Q(last_event_at__lt=cutoff) | Q(last_event_at__isnull=True, created_at__lt=cutoff))
            .values_list("pk", flat=True)[:limit]
        ]
        message = "Agent disconnected and the task's fate stayed unknown — expired."
        for pk in disconnected:
            if await self._claim(
                pk,
                to_kind=enums.TaskEventKind.CRITICAL,
                mark_done=True,
                only_if=lambda t: t.latest_event_kind == enums.TaskEventKind.DISCONNECTED,
                event={"message": message},
                skip_locked=True,
            ):
                await self._unfold_to_higher_order(str(pk), enums.TaskEventKind.CRITICAL, message=message)
                expired += 1

        undelivered = [
            pk
            async for pk in models.Task.objects.filter(is_done=False, picked_up_at__isnull=True, latest_event_kind=enums.TaskEventKind.QUEUED, agent__kind=enums.AgentKind.WEBSOCKET.value)
            .filter(Q(dispatched_at__lt=cutoff) | Q(dispatched_at__isnull=True, created_at__lt=cutoff))
            .exclude(liveness.live_agent_q("agent"))
            .exclude(implementation__higher_order_for__isnull=False)
            .values_list("pk", flat=True)[:limit]
        ]
        message = "The agent never came back to pick this task up — expired."
        for pk in undelivered:
            if await self._claim(
                pk,
                to_kind=enums.TaskEventKind.CRITICAL,
                mark_done=True,
                only_if=lambda t: t.picked_up_at is None and t.latest_event_kind == enums.TaskEventKind.QUEUED and (t.dispatched_at or t.created_at) < cutoff,
                event={"message": message},
                skip_locked=True,
            ):
                await self._unfold_to_higher_order(str(pk), enums.TaskEventKind.CRITICAL, message=message)
                expired += 1
        return expired

    async def on_agent_state_patch(self, agent_id: int, message: messages.StatePatch) -> None:
        logging.info(f"Log Patch for Task {message.state_name}")

        state = await models.State.objects.aget(agent_id=agent_id, interface=message.state_name)
        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)

        await models.Patch.objects.acreate(
            state=state,
            agent_id=agent_id,
            session=session,
            interface=message.state_name,
            op=message.op,
            path=message.path,
            value=message.value,
            task_id=message.task_id,
            global_rev=message.global_rev,
        )

    async def on_agent_state_snapshot(self, agent_id: int, message: messages.StateSnapshot) -> None:
        logging.info(f"Log Snapshot for Task {agent_id}")

        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)
        agent = await models.Agent.objects.aget(id=agent_id)

        for state_name, snapshot in message.snapshots.items():
            state = await models.State.objects.aget(agent_id=agent_id, interface=state_name)

            await models.Snapshot.objects.acreate(
                session=session,
                state=state,
                agent=agent,
                value=snapshot,
                global_rev=message.global_rev,
            )

    async def on_agent_session_init(self, agent_id: int, message: messages.SessionInit) -> None:
        logging.info(f"Session init {message.session_id} with data {message}")
        # For now we don't do anything with this, but it could be used to initialize session-specific data

        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)
        agent = await models.Agent.objects.aget(id=agent_id)

        for state_name, snapshot in message.states.items():
            state = await models.State.objects.aget(agent_id=agent_id, interface=state_name)

            await models.Snapshot.objects.acreate(
                session=session,
                state=state,
                agent=agent,
                value=snapshot,
                global_rev=0,
            )

    async def on_agent_lock(self, agent_id: int, message: messages.Lock) -> None:
        # Acquire: record that ``task`` holds lock ``key`` on this agent. Lock rows are
        # normally pre-created at registration; aupdate_or_create tolerates a missing one.
        # An unknown task is ignored (a stray lock must not tear down the transport, and
        # setting a dangling FK would raise IntegrityError → socket close).
        if not await models.Task.objects.filter(pk=message.task).aexists():
            logging.warning(f"Lock {message.key} requested by unknown task {message.task} — ignored")
            return
        await models.Lock.objects.aupdate_or_create(
            agent_id=agent_id,
            key=message.key,
            defaults={"hold_by_id": message.task},
        )

    async def on_agent_unlock(self, agent_id: int, message: messages.Unlock) -> None:
        # Release: clear the holder (no-op if the lock is absent or already free).
        await models.Lock.objects.filter(agent_id=agent_id, key=message.key).aupdate(hold_by=None)


persist_backend = ModelPersistBackend()
