"""Refactor surface: typed port conformance, the delivery seam, pure reconcile ops, the sweep.

These pin the new abstractions the real-time refactor introduced. The existing
``test_reclaim_grace.py`` still exercises the *timer* trigger over the same reconcile ops;
here we call the ops directly and drive the DB sweeps the in-process reaper runs.
"""

from datetime import timedelta
import pytest
from django.utils import timezone

from facade import enums, messages, transport
from facade.ports import PersistBackend
from facade.persist_backend import ModelPersistBackend, persist_backend

from tests.factories import build_task, build_webhook_agent


def test_persist_backend_satisfies_port():
    # runtime_checkable Protocol: the concrete backend exposes the whole port surface.
    assert isinstance(persist_backend, PersistBackend)


def test_deliver_to_agent_routes_by_kind(monkeypatch):
    pushed, posted = [], []
    monkeypatch.setattr(transport.RedisAgentQueue, "from_settings", classmethod(lambda cls: type("Q", (), {"push": lambda self, a, b, priority=False: pushed.append((a, b))})()))
    monkeypatch.setattr(transport.hooks, "deliver_to_hook", lambda agent, body: posted.append((agent, body)) or True)

    ws = type("A", (), {"pk": 7, "kind": enums.AgentKind.WEBSOCKET.value})()
    hook = type("A", (), {"pk": 8, "kind": enums.AgentKind.WEBHOOK.value})()
    msg = messages.Cancel(task="a1")

    assert transport.deliver_to_agent(ws, msg) is True
    assert transport.deliver_to_agent(hook, msg) is True

    assert len(pushed) == 1 and pushed[0][0] == "7"
    assert len(posted) == 1 and posted[0][0] is hook


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestReconcileOps:
    async def test_reconcile_orphaned_executor_work_is_effect_aware(self):
        none_ass = await build_task("rec-none", effect="NONE")
        phys_ass = await build_task("rec-phys", effect="PHYSICAL")
        backend = ModelPersistBackend()

        # Mark both agents disconnected (the reconcile op no-ops a connected agent).
        from facade.models import Agent

        for ass in (none_ass, phys_ass):
            await Agent.objects.filter(pk=ass.agent_id).aupdate(connected=False)
            await backend.reconcile_orphaned_executor_work(ass.agent_id)

        none_ass = await type(none_ass).objects.aget(pk=none_ass.pk)
        phys_ass = await type(phys_ass).objects.aget(pk=phys_ass.pk)
        assert none_ass.latest_event_kind == enums.TaskEventKind.DISCONNECTED and none_ass.is_done is False
        assert phys_ass.latest_event_kind == enums.TaskEventKind.CRITICAL and phys_ass.is_done is True


@pytest.mark.django_db(transaction=True)
class TestReconcileSweep:
    def test_sweep_fails_stale_disconnected_executor_work(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 30, "PHYSICAL": 30}
        from asgiref.sync import async_to_sync

        from facade.models import Agent, Task

        ass = async_to_sync(build_task)("sweep-stale", effect="NONE")
        # Disconnected websocket executor, gone well past the grace window.
        Agent.objects.filter(pk=ass.agent_id).update(kind=enums.AgentKind.WEBSOCKET.value, connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        async_to_sync(ModelPersistBackend().reconcile_disconnected_agents)()

        refreshed = Task.objects.get(pk=ass.pk)
        assert refreshed.latest_event_kind == enums.TaskEventKind.DISCONNECTED

    def test_sweep_leaves_connected_and_webhook_untouched(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 30, "PHYSICAL": 30}
        from asgiref.sync import async_to_sync

        from facade.models import Agent, Task

        connected = async_to_sync(build_task)("sweep-conn", effect="NONE")
        Agent.objects.filter(pk=connected.agent_id).update(kind=enums.AgentKind.WEBSOCKET.value, connected=True, last_seen=timezone.now())
        webhook = async_to_sync(build_task)("sweep-hook", effect="NONE")
        Agent.objects.filter(pk=webhook.agent_id).update(kind=enums.AgentKind.WEBHOOK.value, connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        async_to_sync(ModelPersistBackend().reconcile_disconnected_agents)()

        assert Task.objects.get(pk=connected.pk).latest_event_kind == enums.TaskEventKind.STARTED
        assert Task.objects.get(pk=webhook.pk).latest_event_kind == enums.TaskEventKind.STARTED


@pytest.mark.django_db(transaction=True)
class TestReconcileStaleAgents:
    """The reaper: heal websocket agents whose ``connected`` is stuck True past the stale window."""

    def test_stale_connected_agent_is_reaped_and_work_reconciled(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 30, "PHYSICAL": 30}
        from asgiref.sync import async_to_sync

        from facade.models import Agent, Task

        ass = async_to_sync(build_task)("reap-stale", effect="NONE")
        # connected stuck True but the heartbeat expired — the crashed-worker case.
        Agent.objects.filter(pk=ass.agent_id).update(kind=enums.AgentKind.WEBSOCKET.value, connected=True, last_seen=timezone.now() - timedelta(minutes=5))

        healed = async_to_sync(persist_backend.reconcile_stale_agents)()

        assert healed == 1
        assert Agent.objects.get(pk=ass.agent_id).connected is False
        # its orphaned in-flight work is reconciled in the same pass.
        assert Task.objects.get(pk=ass.pk).latest_event_kind == enums.TaskEventKind.DISCONNECTED

    def test_fresh_connected_agent_not_reaped(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 30, "PHYSICAL": 30}
        from asgiref.sync import async_to_sync

        from facade.models import Agent, Task

        ass = async_to_sync(build_task)("reap-fresh", effect="NONE")
        Agent.objects.filter(pk=ass.agent_id).update(kind=enums.AgentKind.WEBSOCKET.value, connected=True, last_seen=timezone.now())

        healed = async_to_sync(persist_backend.reconcile_stale_agents)()

        assert healed == 0
        assert Agent.objects.get(pk=ass.agent_id).connected is True
        assert Task.objects.get(pk=ass.pk).latest_event_kind == enums.TaskEventKind.STARTED

    def test_reaper_heal_fires_agent_subscription(self, settings, monkeypatch):
        # The heal must go through Model.save() so agent_post_save fires and the GraphQL
        # agents/active subscriptions (and dashboards) refresh to reality.
        settings.REKUEST_GRACE = {"DEFAULT": 30, "PHYSICAL": 30}
        from asgiref.sync import async_to_sync

        from facade import channels
        from facade.models import Agent

        ass = async_to_sync(build_task)("reap-signal", effect="NONE")
        Agent.objects.filter(pk=ass.agent_id).update(kind=enums.AgentKind.WEBSOCKET.value, connected=True, last_seen=timezone.now() - timedelta(minutes=5))

        events = []
        # Patch after seeding so only the heal save is captured.
        monkeypatch.setattr(channels.agent_updated_channel, "broadcast", lambda event, groups=None: events.append((event, groups)))

        async_to_sync(persist_backend.reconcile_stale_agents)()

        assert any(getattr(e, "update", None) == ass.agent_id for e, _ in events)
