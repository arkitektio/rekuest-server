"""What an executing agent tells us about its work, and the fence around it.

Two things every handler here depends on. The agent's *socket* is authenticated but the task id
inside a frame is not, so :meth:`_agent_task` resolves every reported task with an ``agent_id``
predicate — without it any authenticated agent could terminate or inject results into any task in
any organization by naming its id. And that same method is the one place that can answer "did the
agent ever pick this up?", because the stream events (log / yield / progress) append to the log
without moving ``latest_event_kind``: a healthy running task still reads ``QUEUED``.
"""

import logging
from typing import Any, Dict

from django.utils import timezone

from facade import models, enums, messages
from facade.deadlines import (
    progress_lease_seconds,
)

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

    async def _record_event(self, agent_id: int, task_id: str, kind: str, **fields: Any) -> bool:
        """Append a non-terminal event to a task this agent owns. Returns whether it was recorded.

        The stream events (log / yield / progress) are fire-and-forget: they append to the log and
        deliberately do NOT move ``latest_event_kind``, so a running task keeps reading ``QUEUED``
        (``picked_up_at``, stamped by ``_agent_task``, is what records that it was picked up). An
        event for an unknown task — or another agent's — is dropped rather than tearing down the
        transport.
        """
        if await self._agent_task(agent_id, task_id) is None:
            return False
        await models.TaskEvent.objects.acreate(task_id=task_id, kind=kind, **fields)
        return True

    async def on_agent_log(self, agent_id: int, message: messages.Log) -> None:
        logger.debug("Log for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._record_event(agent_id, message.task, enums.TaskEventKind.LOG, message=message.message, level=message.level)

    async def on_agent_yield(self, agent_id: int, message: messages.Yield) -> None:
        logger.debug("Yield for task %s (seq %s)", message.task, getattr(message, "seq", None))
        if await self._record_event(agent_id, message.task, enums.TaskEventKind.YIELD, returns=message.returns):
            await self._unfold_to_higher_order(message.task, enums.TaskEventKind.YIELD, returns=message.returns)

    async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None:
        logger.debug("Completed for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.COMPLETED)

    async def on_agent_cancelled(self, agent_id: int, message: messages.Cancelled) -> None:
        logger.debug("Cancelled for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.CANCELLED)

    async def on_agent_error(self, agent_id: int, message: messages.Failed) -> None:
        logger.debug("Failed for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.FAILED, message=message.error)

    async def on_agent_critical(self, agent_id: int, message: messages.Critical) -> None:
        logger.debug("Critical for task %s (seq %s)", message.task, getattr(message, "seq", None))
        await self._finalize_from_agent(agent_id, message.task, enums.TaskEventKind.CRITICAL, message=message.error)

    async def on_agent_progress(self, agent_id: int, message: messages.Progress) -> None:
        logger.debug("Progress for task %s (seq %s)", message.task, getattr(message, "seq", None))
        if await self._record_event(agent_id, message.task, enums.TaskEventKind.PROGRESS, progress=message.progress, message=message.message):
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
