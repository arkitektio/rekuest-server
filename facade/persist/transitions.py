"""The task transition primitive — the kernel every other part of the backend builds on.

One place decides "is this task still open, and may I move it?", and it decides it under a row
lock, writing the ``TaskEvent`` in the same transaction. That matters because sweeps are
re-entrant AND run in every backend process, while an agent retries an unacked terminal report
over whatever connection it has next: reading the flag and then writing it lets two backends both
observe an open task and both emit a terminal event for it.

This module calls nothing else in the backend. Everything else calls it.
"""

import logging
from typing import Any, Callable, Dict, Optional

from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from facade import models, enums
from facade.higher_order import project_returns

logger = logging.getLogger(__name__)

_TERMINAL_KINDS = (
    enums.TaskEventKind.COMPLETED,
    enums.TaskEventKind.CANCELLED,
    enums.TaskEventKind.INTERRUPTED,
    enums.TaskEventKind.FAILED,
    enums.TaskEventKind.CRITICAL,
)


class TaskTransitionMixin:
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

    async def _finalize_terminal(
        self,
        task_id: int,
        kind: str,
        message: str,
        *,
        only_if: Callable[[models.Task], bool] | None = None,
        extra: Dict[str, Any] | None = None,
        skip_locked: bool = False,
        task: models.Task | None = None,
    ) -> bool:
        """Finalize a task the SERVER decided is over, and project it onto any wrapper.

        Every server-side terminal — a lost executor, an unconfirmed interrupt, a silent physical
        op, an expired task — is the same two steps: claim the transition (which writes the
        ``TaskEvent`` in the same transaction) and, only if this backend won the claim, unfold the
        outcome onto a higher-order wrapper. Doing the unfold outside the win check would let two
        backends both project the same terminal onto the wrapper.

        Returns whether we won. ``skip_locked`` defaults to False, matching :meth:`_claim`: a sweep
        stepping over a row another backend holds passes True, a single-task op does not.

        Not for an agent-*reported* terminal — that is :meth:`_finalize_from_agent`, which has to
        resolve and authenticate the task first.
        """
        won = await self._claim(
            task_id,
            to_kind=kind,
            mark_done=True,
            only_if=only_if,
            extra=extra,
            event={"message": message},
            skip_locked=skip_locked,
        )
        if won:
            await self._unfold_to_higher_order(str(task_id), kind, message=message, task=task)
        return won

    @staticmethod
    def _effect_of(task: models.Task) -> str:
        """The task's effect class — decides the retry axis (physical work is never re-run)."""
        implementation = task.implementation
        return implementation.effect if implementation is not None else enums.EffectClassChoices.NONE.value
