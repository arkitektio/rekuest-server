"""What an executing agent tells us about its work, and the fence around it.

Two things every handler here depends on. The agent's *socket* is authenticated but the task id
inside a frame is not, so :meth:`_agent_task` resolves every reported task with an ``agent_id``
predicate — without it any authenticated agent could terminate or inject results into any task in
any organization by naming its id. And that same method is the one place that can answer "did the
agent ever pick this up?", because the stream events (log / yield / progress) append to the log
without moving ``latest_event_kind``: a healthy running task still reads ``QUEUED``.
"""

import logging
from typing import Any, Dict, Optional

from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from facade import models, enums, messages
from facade.deadlines import (
    progress_lease_seconds,
)
from facade.persist.positions import is_numbered, position_stamp
from facade.persist.transitions import _TERMINAL_KINDS

logger = logging.getLogger(__name__)


class AgentReportMixin:
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
            logger.warning(f"Agent {agent_id} reported on task {task_id}, which is not assigned to it. Dropping.")
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

    async def _finalize_from_agent(
        self,
        agent_id: int,
        task_id: str,
        kind,
        *,
        message: str | None = None,
        stamp: Dict[str, Any] | None = None,
        journal_session: Optional[str] = None,
    ) -> None:
        """Persist an agent-reported terminal — exactly once, however often it is reported.

        The agent retries a terminal report until it is acked, possibly over a new connection to
        a different backend while the first is still processing it. So the "already done?" check
        and the write are one row-locked claim, and the ``TaskEvent`` is written inside it: two
        backends receiving the same report produce one terminal event. ``stamp`` is the report's
        position/time/step (``position_stamp``), carried on the event.

        **The agent's outcome wins** over one the server wrote itself. A report numbered in an
        earlier session (``journal_session`` is not the agent's current one) arrives after a
        restart, when registering the new session has already orphaned that session's in-flight
        work. If the task's terminal event is server-written (no ``agent_pos``), the report
        replaces it (see ``docs/design/journal.md``).
        """
        x = await self._agent_task(agent_id, task_id, reopen=False)
        if x is None:
            return
        event: Dict[str, Any] = {"message": message} if message is not None else {}
        event.update(stamp or {})
        if x.is_done:
            # A resent terminal report — or the agent's outcome for work the server orphaned.
            if journal_session is None or not await self._from_earlier_session(agent_id, journal_session):
                return
            if not await database_sync_to_async(self._override_server_outcome_sync)(x.pk, kind, event):
                return
        elif not await self._claim(x.pk, to_kind=kind, mark_done=True, event=event):
            return  # lost the race to another report / a sweep — theirs is the outcome
        # A wrapper that is already done is left alone: ``_claim`` refuses done tasks.
        await self._unfold_to_higher_order(task_id, kind, message=message, task=x)

    async def _from_earlier_session(self, agent_id: int, journal_session: str) -> bool:
        active = await models.Agent.objects.filter(pk=agent_id).values_list("active_session_id", flat=True).afirst()
        return active is not None and active != journal_session

    def _override_server_outcome_sync(self, task_id: int, kind: str, event: Dict[str, Any]) -> bool:
        """Replace a done task's server-written terminal with the agent's. Returns whether it did."""
        with transaction.atomic():
            task = models.Task.objects.select_for_update(of=("self",)).filter(pk=task_id).first()
            if task is None or not task.is_done:
                return False
            last = models.TaskEvent.objects.filter(task=task, kind__in=[str(k.value) for k in _TERMINAL_KINDS]).order_by("-id").first()
            if last is None or last.agent_pos is not None:
                return False  # the agent already reported this task's outcome: that one stands
            logger.info("Task %s: the agent's %s (from its previous session) replaces the server's %s", task_id, kind, last.kind)
            task.latest_event_kind = kind
            task.finished_at = timezone.now()
            task.save(update_fields=["latest_event_kind", "finished_at"])
            note = f"Reported by the agent's previous session; replaces the server's {last.kind}."
            models.TaskEvent.objects.create(task=task, kind=kind, **{**event, "message": f"{event['message']} ({note})" if event.get("message") else note})
        return True

    async def _terminal(self, agent_id: int, message: Any, kind, *, error: str | None = None) -> None:
        await self._finalize_from_agent(
            agent_id,
            message.task,
            kind,
            message=error,
            stamp=position_stamp(message),
            journal_session=message.journal_session if is_numbered(message) else None,
        )

    async def on_agent_interrupted(self, agent_id: int, message: messages.Interrupted) -> None:
        await self._terminal(agent_id, message, enums.TaskEventKind.INTERRUPTED)

    async def _on_nonterminal_confirm(self, agent_id: int, task_id: str, kind, *, extra: Dict[str, Any] | None = None, stamp: Dict[str, Any] | None = None) -> None:
        """Persist a non-terminal lifecycle confirmation (started/paused/resumed)."""
        # A confirmation for an unknown task — or another agent's task — must not tear down the
        # transport; it is dropped.
        x = await self._agent_task(agent_id, task_id)
        if x is None:
            return
        if x.is_done:
            return
        await self._claim(x.pk, to_kind=kind, extra=extra, event=dict(stamp or {}))

    async def on_agent_paused(self, agent_id: int, message: messages.Paused) -> None:
        # A suspended op stops reporting progress — don't let the silent-physical-op lease reap
        # it: clearing the stamp takes it out of ``reconcile_silent_physical_ops`` until the
        # next Progress re-arms it.
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.PAUSED, extra={"last_progress_at": None}, stamp=position_stamp(message))

    async def on_agent_resumed(self, agent_id: int, message: messages.Resumed) -> None:
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.RESUMED, stamp=position_stamp(message))

    async def on_agent_started(self, agent_id: int, message: messages.Started) -> None:
        # The agent accepted and began executing — record it (mirrored to the caller as StartedEvent).
        await self._on_nonterminal_confirm(agent_id, message.task, enums.TaskEventKind.STARTED, stamp=position_stamp(message))

    async def _record_event(self, agent_id: int, task_id: str, kind: str, **fields: Any) -> bool:
        """Append a non-terminal event to a task this agent owns. Returns whether it was recorded.

        The stream events (log / yield / progress) are fire-and-forget: they append to the log and
        deliberately do NOT move ``latest_event_kind``, so a running task keeps reading ``QUEUED``
        (``picked_up_at``, stamped by ``_agent_task``, is what records that it was picked up). An
        event for an unknown task — or another agent's — is dropped rather than tearing down the
        transport. Nothing is recorded on a task that is already done: its history ends with its
        terminal event, whatever arrives late (a resend from an earlier session, a report racing
        the server's own finalization).
        """
        x = await self._agent_task(agent_id, task_id)
        if x is None:
            return False
        if x.is_done:
            logger.debug("Dropping a %s for task %s, which is already done", kind, task_id)
            return False
        await models.TaskEvent.objects.acreate(task_id=task_id, kind=kind, **fields)
        return True

    async def on_agent_log(self, agent_id: int, message: messages.Log) -> None:
        logger.debug("Log for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._record_event(agent_id, message.task, enums.TaskEventKind.LOG, message=message.message, level=message.level, **position_stamp(message))

    async def on_agent_yield(self, agent_id: int, message: messages.Yield) -> None:
        logger.debug("Yield for task %s (seq %s)", message.task, getattr(message, "seq", None))
        if await self._record_event(agent_id, message.task, enums.TaskEventKind.YIELD, returns=message.returns, **position_stamp(message)):
            await self._unfold_to_higher_order(message.task, enums.TaskEventKind.YIELD, returns=message.returns)

    async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None:
        logger.debug("Completed for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._terminal(agent_id, message, enums.TaskEventKind.COMPLETED)

    async def on_agent_cancelled(self, agent_id: int, message: messages.Cancelled) -> None:
        logger.debug("Cancelled for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._terminal(agent_id, message, enums.TaskEventKind.CANCELLED)

    async def on_agent_error(self, agent_id: int, message: messages.Failed) -> None:
        logger.debug("Failed for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._terminal(agent_id, message, enums.TaskEventKind.FAILED, error=message.error)

    async def on_agent_critical(self, agent_id: int, message: messages.Critical) -> None:
        logger.debug("Critical for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._terminal(agent_id, message, enums.TaskEventKind.CRITICAL, error=message.error)

    async def on_agent_progress(self, agent_id: int, message: messages.Progress) -> None:
        logger.debug("Progress for task %s (seq %s)", message.task, getattr(message, "seq", None))
        if await self._record_event(agent_id, message.task, enums.TaskEventKind.PROGRESS, progress=message.progress, message=message.message, **position_stamp(message)):
            await self._arm_progress_lease(message.task)

    async def on_agent_effect(self, agent_id: int, message: messages.Effect) -> None:
        """A value the task took from outside itself, kept at its step for a later replay."""
        await self._record_event(agent_id, message.task, enums.TaskEventKind.EFFECT, effect=message.effect, value=message.value, **position_stamp(message))

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
            implementation__effects=enums.EffectsChoices.IRREVERSIBLE.value,
        ).aupdate(last_progress_at=timezone.now())
