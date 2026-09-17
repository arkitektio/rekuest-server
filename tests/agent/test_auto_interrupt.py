"""auto_interrupt escalation + manual escalation, driving ``ModelPersistBackend`` directly.

A cancel that carries ``auto_interrupt=<seconds>`` escalates to an interrupt if the agent
hasn't confirmed within the window; a confirmed (or otherwise terminal) cancel never
escalates; ``None`` disables escalation entirely.

The window is not a timer: it is ``Task.interrupt_at``, acted on by the
``escalate_due_controls`` sweep. The tests arm it on one backend instance and sweep from
another — the escalation must not depend on the process that received the cancel surviving.
"""

import asyncio

import pytest

from facade import enums, messages
from facade.models import Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.factories import build_task_for_agent_caller, seed_agent

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _owned(prefix):
    """A seeded agent + a task whose caller is that agent (so it may control it)."""
    agent = await seed_agent(f"{prefix}-agent")
    ass = await build_task_for_agent_caller(agent.pk, prefix)
    return agent, ass


async def _kinds(ass_id):
    return [e.kind async for e in TaskEvent.objects.filter(task_id=ass_id)]


class TestAutoInterrupt:
    async def test_fires_and_escalates_to_interrupt(self):
        agent, ass = await _owned("ai-fire")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=key, auto_interrupt=0.05))
        assert (await Task.objects.aget(pk=ass.pk)).interrupt_at is not None  # CANCELING persisted, deadline armed
        assert await ModelPersistBackend().escalate_due_controls() == 0  # not due yet
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().escalate_due_controls() == 1  # another backend fires it

        kinds = await _kinds(ass.pk)
        assert enums.TaskEventKind.CANCELLING in kinds
        assert enums.TaskEventKind.INTERRUPTING in kinds  # escalated
        assert (await Task.objects.aget(pk=ass.pk)).interrupt_at is None

    async def test_concurrent_sweeps_escalate_once(self):
        agent, ass = await _owned("ai-race")
        key = str(ass.pk)
        await ModelPersistBackend().on_caller_cancel(agent.pk, messages.CancelRequest(task=key, auto_interrupt=0.01))
        await asyncio.sleep(0.05)

        # N backends sweep at the same instant: the deadline is claimed by exactly one.
        results = await asyncio.gather(*(ModelPersistBackend().escalate_due_controls() for _ in range(4)))

        assert sum(results) == 1
        assert (await _kinds(ass.pk)).count(enums.TaskEventKind.INTERRUPTING) == 1

    async def test_none_disables_escalation(self):
        agent, ass = await _owned("ai-none")
        backend = ModelPersistBackend()
        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=str(ass.pk)))
        assert (await Task.objects.aget(pk=ass.pk)).interrupt_at is None
        assert await backend.escalate_due_controls() == 0

    async def test_confirm_before_window_cancels_timer(self):
        agent, ass = await _owned("ai-confirm")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=key, auto_interrupt=0.05))
        await backend.on_agent_cancelled(agent.pk, messages.Cancelled(task=key))
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().escalate_due_controls() == 0  # terminal work has no deadline
        assert enums.TaskEventKind.INTERRUPTING not in await _kinds(ass.pk)

    async def test_escalation_is_noop_if_already_terminal(self):
        agent, ass = await _owned("ai-terminal")
        await Task.objects.filter(pk=ass.pk).aupdate(is_done=True)
        backend = ModelPersistBackend()

        await backend._escalate_to_interrupt(str(ass.pk))  # re-reads is_done → no-op

        assert enums.TaskEventKind.INTERRUPTING not in await _kinds(ass.pk)

    async def test_terminal_by_other_path_cancels_timer(self):
        agent, ass = await _owned("ai-other")
        backend = ModelPersistBackend()
        key = str(ass.pk)
        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=key, auto_interrupt=0.05))
        await backend.on_agent_done(agent.pk, messages.Completed(task=key))
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().escalate_due_controls() == 0
        assert enums.TaskEventKind.INTERRUPTING not in await _kinds(ass.pk)


class TestControlDeadline:
    """The global control deadline covers the GraphQL path, which carries no ``auto_interrupt``."""

    async def test_unconfirmed_cancel_escalates_then_interrupt_is_finalized(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "CONTROL_DEADLINE": 0.05}
        agent, ass = await _owned("cd")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=key))  # no auto_interrupt
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().escalate_due_controls() == 1
        assert enums.TaskEventKind.INTERRUPTING in await _kinds(ass.pk)

        # The interrupt is never confirmed either: the server stops waiting.
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().escalate_due_controls() == 1
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True and refreshed.latest_event_kind == enums.TaskEventKind.INTERRUPTED


class TestManualEscalation:
    async def test_manual_cancel_then_interrupt(self):
        agent, ass = await _owned("me")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_caller_cancel(agent.pk, messages.CancelRequest(task=key))  # CANCELING, not terminal
        await backend.on_caller_interrupt(agent.pk, messages.InterruptRequest(task=key))  # INTERRUPTING

        kinds = await _kinds(ass.pk)
        assert enums.TaskEventKind.CANCELLING in kinds and enums.TaskEventKind.INTERRUPTING in kinds

        await backend.on_agent_interrupted(agent.pk, messages.Interrupted(task=key))
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True and refreshed.latest_event_kind == enums.TaskEventKind.INTERRUPTED
