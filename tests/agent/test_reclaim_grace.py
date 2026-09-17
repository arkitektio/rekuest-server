"""Reclaim / grace / caller-death cascade — the concurrency core of the liveness model.

These drive the ``ModelPersistBackend`` port directly with seeded tasks and the pytest-django
``settings`` fixture to control the grace window. They target the session-match and
grace-expiry branches the plan flagged as the genuine concurrency risk.

The backend is stateless: a grace window is not a timer but ``Agent.last_seen`` aging past the
window, acted on by the ``reconcile_disconnected_agents`` sweep. So the tests deliberately use
TWO backend instances — the disconnect lands on one "process", the sweep runs on another —
which is exactly the property that makes a backend restart (or N backends) safe.

There is no caller-death cascade any more: every socket connection is an agent, roots
originate only from the GraphQL ``assign`` mutation, and a dependent task's fate follows its
parent — so agent death (above) is the only disconnect cascade left.
"""

import asyncio

import pytest

from facade import enums, messages
from facade.models import Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.factories import build_task

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


def _grace(settings, value):
    settings.REKUEST_GRACE = {"DEFAULT": value, "PHYSICAL": value}


async def _event_kinds(ass_id):
    return [e.kind async for e in TaskEvent.objects.filter(task_id=ass_id)]


async def _expire_grace(window=0.05):
    """Let the grace window pass, then sweep from a DIFFERENT backend instance."""
    await asyncio.sleep(window * 3)
    return await ModelPersistBackend().reconcile_disconnected_agents()


class TestExecutorReclaim:
    async def test_same_session_reconnect_reclaims(self, settings):
        _grace(settings, 30)
        ass = await build_task("recl-same")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await ModelPersistBackend().reconcile_disconnected_agents() == 0  # inside the window: held

        # The reconnect lands on another backend — there is no timer to find and cancel.
        claim = await ModelPersistBackend().on_agent_connected(agent_id, "c2", session_id="S1")
        assert claim.claimed
        assert await ModelPersistBackend().reconcile_disconnected_agents() == 0  # live again
        assert any(str(a.pk) == str(ass.pk) for a in claim.tasks)  # handed back as inquiry

        assert enums.TaskEventKind.DISCONNECTED not in await _event_kinds(ass.pk)
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is False

    async def test_different_session_fails_orphaned_work(self, settings):
        _grace(settings, 30)
        ass = await build_task("recl-diff", effect="NONE")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        # A fresh process (different session) took over: the old work is orphaned.
        claim = await backend.on_agent_connected(agent_id, "c2", session_id="S2")

        assert claim.claimed
        assert claim.tasks == []
        assert enums.TaskEventKind.DISCONNECTED in await _event_kinds(ass.pk)


class TestExecutorGraceExpiry:
    async def test_none_effect_expiry_is_recoverable_disconnected(self, settings):
        _grace(settings, 0.05)
        ass = await build_task("recl-exp-none", effect="NONE")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        assert enums.TaskEventKind.DISCONNECTED in await _event_kinds(ass.pk)
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is False  # none-effect is recoverable

    async def test_physical_effect_expiry_is_terminal_critical(self, settings):
        _grace(settings, 0.05)
        ass = await build_task("recl-exp-phys", effect="PHYSICAL")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        assert enums.TaskEventKind.CRITICAL in await _event_kinds(ass.pk)
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True  # physical ambiguous failure is terminal

    async def test_reconnect_before_expiry_prevents_failure(self, settings):
        _grace(settings, 30)
        ass = await build_task("recl-noexp")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)
        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.on_agent_connected(agent_id, "c2", session_id="S1")  # reclaim

        assert await _event_kinds(ass.pk) == []  # no failure event ever fired


class TestProgressLease:
    async def test_silent_physical_op_fails_terminal(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 0.05}
        ass = await build_task("lease-phys", effect="PHYSICAL")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=key, progress=10))
        assert (await Task.objects.aget(pk=ass.pk)).last_progress_at is not None  # lease armed (a column)
        await asyncio.sleep(0.15)
        assert await ModelPersistBackend().reconcile_silent_physical_ops() == 1  # fired by another backend

        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True
        assert refreshed.latest_event_kind == enums.TaskEventKind.CRITICAL

    async def test_fresh_progress_rearms_lease(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 30}
        ass = await build_task("lease-rearm", effect="PHYSICAL")
        backend = ModelPersistBackend()

        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=str(ass.pk), progress=10))
        assert await backend.reconcile_silent_physical_ops() == 0
        assert (await Task.objects.aget(pk=ass.pk)).is_done is False

    async def test_paused_op_is_not_reaped(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 0.05}
        ass = await build_task("lease-paused", effect="PHYSICAL")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=key, progress=10))
        await backend.on_agent_paused(ass.agent_id, messages.Paused(task=key))
        await asyncio.sleep(0.15)
        assert await backend.reconcile_silent_physical_ops() == 0  # suspended ops report nothing
        assert (await Task.objects.aget(pk=ass.pk)).is_done is False

    async def test_done_clears_lease_no_failure(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 30}
        ass = await build_task("lease-done", effect="PHYSICAL")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=key, progress=10))
        await backend.on_agent_done(ass.agent_id, messages.Completed(task=key))
        # A terminal task is out of the sweep by construction (is_done) — there is no lease to
        # clear: even with its stamp long past the window, nothing fires.
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 0.01}
        await asyncio.sleep(0.05)
        assert await backend.reconcile_silent_physical_ops() == 0
        assert (await Task.objects.aget(pk=ass.pk)).latest_event_kind == enums.TaskEventKind.COMPLETED

    async def test_none_effect_has_no_lease(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PHYSICAL": 0, "PROGRESS_LEASE": 0.05}
        ass = await build_task("lease-none", effect="NONE")
        backend = ModelPersistBackend()
        key = str(ass.pk)

        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=key, progress=10))
        assert (await Task.objects.aget(pk=ass.pk)).last_progress_at is None  # only physical work gets a lease
        await asyncio.sleep(0.15)
        assert await backend.reconcile_silent_physical_ops() == 0


class TestIdempotentRedispatch:
    """The third level of the retry axis: PHYSICAL (terminal) < default (fate unknown) <
    idempotent (freely re-dispatchable — QUEUED + Assign re-broadcast into the agent queue,
    which retains messages for offline agents)."""

    @pytest.fixture
    def broadcasts(self, monkeypatch):
        from facade.consumers.async_consumer import AgentConsumer

        recorded = []
        monkeypatch.setattr(AgentConsumer, "broadcast", staticmethod(lambda agent_id, message: recorded.append((agent_id, message))))
        return recorded

    async def test_idempotent_expiry_requeues(self, settings, broadcasts):
        _grace(settings, 0.05)
        ass = await build_task("recl-idem", effect="NONE", idempotent=True)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        kinds = await _event_kinds(ass.pk)
        assert enums.TaskEventKind.QUEUED in kinds
        assert enums.TaskEventKind.DISCONNECTED not in kinds
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is False
        assert refreshed.latest_event_kind == enums.TaskEventKind.QUEUED

        assert len(broadcasts) == 1
        target_agent, message = broadcasts[0]
        assert isinstance(message, messages.Assign)
        assert message.task == str(ass.pk)
        assert message.args == (ass.args or {})
        assert message.reference == str(ass.reference)

    async def test_idempotent_physical_still_terminal(self, settings, broadcasts):
        _grace(settings, 0.05)
        ass = await build_task("recl-idem-phys", effect="PHYSICAL", idempotent=True)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is True
        assert refreshed.latest_event_kind == enums.TaskEventKind.CRITICAL
        assert broadcasts == []

    async def test_sweep_is_reentrant_single_requeue(self, settings, broadcasts):
        _grace(settings, 30)
        ass = await build_task("recl-idem-sweep", effect="NONE", idempotent=True)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        # The periodic sweep may reconcile repeatedly — only ONE requeue may result.
        await backend.reconcile_orphaned_executor_work(agent_id)
        await backend.reconcile_orphaned_executor_work(agent_id)

        kinds = await _event_kinds(ass.pk)
        assert kinds.count(enums.TaskEventKind.QUEUED) == 1
        assert len(broadcasts) == 1

    async def test_callerless_idempotent_falls_back_disconnected(self, settings, broadcasts):
        _grace(settings, 30)
        ass = await build_task("recl-idem-nocaller", effect="NONE", idempotent=True)
        await Task.objects.filter(pk=ass.pk).aupdate(caller=None)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.reconcile_orphaned_executor_work(agent_id)

        kinds = await _event_kinds(ass.pk)
        assert enums.TaskEventKind.DISCONNECTED in kinds
        assert enums.TaskEventKind.QUEUED not in kinds
        assert broadcasts == []
