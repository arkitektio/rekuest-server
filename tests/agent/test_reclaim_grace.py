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
    settings.REKUEST_GRACE = {"DEFAULT": value}


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

        assert enums.TaskEventKind.LOST not in await _event_kinds(ass.pk)
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert refreshed.is_done is False

    async def test_a_fresh_process_leaves_the_old_work_lost(self, settings):
        _grace(settings, 30)
        ass = await build_task("recl-diff", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        # A fresh process (different session) took over: the old work is orphaned.
        claim = await backend.on_agent_connected(agent_id, "c2", session_id="S2")

        assert claim.claimed
        assert claim.tasks == []
        refreshed = await Task.objects.aget(pk=ass.pk)
        assert (refreshed.latest_event_kind, refreshed.is_done) == (enums.TaskEventKind.LOST, True)


class TestExecutorGraceExpiry:
    async def test_expiry_ends_the_work_lost_with_what_is_known(self, settings):
        _grace(settings, 0.05)
        ass = await build_task("recl-exp", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_progress(ass.agent_id, messages.Progress(task=str(ass.pk), progress=40))
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        refreshed = await Task.objects.aget(pk=ass.pk)
        assert (refreshed.latest_event_kind, refreshed.is_done) == (enums.TaskEventKind.LOST, True)
        lost = await TaskEvent.objects.aget(task_id=ass.pk, kind=enums.TaskEventKind.LOST)
        assert lost.value == {"started": True, "last_progress": 40, "effects": "UNKNOWN", "reason": lost.message}

    async def test_irreversible_work_ends_lost_too_its_effects_are_only_information(self, settings):
        _grace(settings, 0.05)
        ass = await build_task("recl-exp-irrev", effects="IRREVERSIBLE")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        lost = await TaskEvent.objects.aget(task_id=ass.pk, kind=enums.TaskEventKind.LOST)
        assert (lost.value["started"], lost.value["effects"]) == (True, "IRREVERSIBLE")

    async def test_reconnect_before_expiry_prevents_failure(self, settings):
        _grace(settings, 30)
        ass = await build_task("recl-noexp")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)
        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.on_agent_connected(agent_id, "c2", session_id="S1")  # reclaim

        assert await _event_kinds(ass.pk) == []  # no failure event ever fired


class TestNothingIsReRunOnItsOwn:
    """Whoever called decides what to do with a lost task. The server re-runs nothing,
    even for an action that declares itself idempotent."""

    @pytest.fixture
    def broadcasts(self, monkeypatch):
        from facade.consumers.async_consumer import AgentConsumer

        recorded = []
        monkeypatch.setattr(AgentConsumer, "broadcast", staticmethod(lambda agent_id, message: recorded.append((agent_id, message))))
        return recorded

    async def test_an_idempotent_action_is_lost_not_requeued(self, settings, broadcasts):
        _grace(settings, 0.05)
        ass = await build_task("recl-idem", effects="UNKNOWN", idempotent=True)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        assert await _expire_grace() == 1

        kinds = await _event_kinds(ass.pk)
        assert kinds.count(enums.TaskEventKind.LOST) == 1 and enums.TaskEventKind.QUEUED not in kinds
        assert broadcasts == []

    async def test_repeated_reconciles_end_it_lost_once(self, settings, broadcasts):
        _grace(settings, 30)
        ass = await build_task("recl-reentrant", effects="UNKNOWN")
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.reconcile_orphaned_executor_work(agent_id)
        await backend.reconcile_orphaned_executor_work(agent_id)

        assert (await _event_kinds(ass.pk)).count(enums.TaskEventKind.LOST) == 1
        assert broadcasts == []

    async def test_work_without_a_caller_is_lost_too(self, settings, broadcasts):
        _grace(settings, 30)
        ass = await build_task("recl-nocaller", effects="UNKNOWN")
        await Task.objects.filter(pk=ass.pk).aupdate(caller=None)
        backend = ModelPersistBackend()
        agent_id = str(ass.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.reconcile_orphaned_executor_work(agent_id)

        assert (await Task.objects.aget(pk=ass.pk)).latest_event_kind == enums.TaskEventKind.LOST
        assert broadcasts == []
