"""The sweeps: every deadline the server enforces, acted on from any backend.

None of these is a timer. Each starts at a database column — ``Agent.last_seen``,
``Task.dispatched_at``, ``interrupt_at``, ``last_progress_at`` — so a backend can be killed
mid-window without losing a pending decision, and any number of backends may sweep concurrently.
:mod:`facade.reaper` is what drives them.

Every transition goes through the row-locked claim in :class:`TaskTransitionMixin`, and candidate
scans use ``skip_locked`` so a backend steps over a row another one is already handling rather than
queueing behind it.
"""

import logging
from datetime import timedelta
from typing import List, Tuple

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from facade import liveness, models, enums, messages
from facade.probes.persist import probe_event_backend
from facade.deadlines import (
    disconnected_expiry_seconds,
    grace_seconds,
    pickup_deadline_seconds,
    progress_lease_seconds,
)

logger = logging.getLogger(__name__)

# The pickup watchdog's budget: the original dispatch plus ONE redelivery. Failed handoffs
# (redis down, webhook 5xx) count — otherwise a permanently broken transport never fails.
MAX_DISPATCH_ATTEMPTS = 2


class ReconcileMixin:
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
                await self._finalize_terminal(
                    task.pk,
                    enums.TaskEventKind.CRITICAL,
                    "Executor lost while running physical-effect work — terminal, not retried.",
                    task=task,
                )
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
                if await self._finalize_terminal(
                    task_id,
                    enums.TaskEventKind.INTERRUPTED,
                    "Interrupt was never confirmed by the agent — finalized by the server.",
                    only_if=lambda t, deadline=deadline: t.interrupt_at == deadline,
                    extra={"interrupt_at": None},
                    skip_locked=True,
                ):
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

    async def reconcile_silent_physical_op(self, task_id: str | int, *, cutoff=None) -> bool:
        """Fail a physical task that reported progress then went silent. Claim-based DB op."""
        return await self._finalize_terminal(
            int(task_id),
            enums.TaskEventKind.CRITICAL,
            "Physical op went silent past its progress lease — terminal, not retried.",
            # Re-checked under the lock: a Progress that landed since the scan re-armed the lease.
            only_if=(lambda t: t.last_progress_at is not None and t.last_progress_at < cutoff) if cutoff is not None else None,
            skip_locked=cutoff is not None,
        )

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

            # Deliberately NOT ``_finalize_terminal``: we are already inside this row's
            # ``select_for_update`` above, and the helper's claim would open a nested transaction
            # and re-lock the row we hold. Same three writes, done under the lock we already have.
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
        for pk in disconnected:
            if await self._finalize_terminal(
                pk,
                enums.TaskEventKind.CRITICAL,
                "Agent disconnected and the task's fate stayed unknown — expired.",
                only_if=lambda t: t.latest_event_kind == enums.TaskEventKind.DISCONNECTED,
                skip_locked=True,
            ):
                expired += 1

        undelivered = [
            pk
            async for pk in models.Task.objects.filter(is_done=False, picked_up_at__isnull=True, latest_event_kind=enums.TaskEventKind.QUEUED, agent__kind=enums.AgentKind.WEBSOCKET.value)
            .filter(Q(dispatched_at__lt=cutoff) | Q(dispatched_at__isnull=True, created_at__lt=cutoff))
            .exclude(liveness.live_agent_q("agent"))
            .exclude(implementation__higher_order_for__isnull=False)
            .values_list("pk", flat=True)[:limit]
        ]
        for pk in undelivered:
            if await self._finalize_terminal(
                pk,
                enums.TaskEventKind.CRITICAL,
                "The agent never came back to pick this task up — expired.",
                only_if=lambda t: t.picked_up_at is None and t.latest_event_kind == enums.TaskEventKind.QUEUED and (t.dispatched_at or t.created_at) < cutoff,
                skip_locked=True,
            ):
                expired += 1
        return expired
