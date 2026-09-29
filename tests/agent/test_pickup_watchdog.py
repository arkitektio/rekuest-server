"""The pickup watchdog + expiry: no task waits forever on an agent.

Every other safety net keys on the agent looking *dead*. These cover the rest — the Assign was
lost (dead drain loop, a displaced connection that swallowed the frame, a push that failed after
the row committed, a webhook that was down) or the agent dropped it silently — while the agent
keeps answering heartbeats.

Real postgres + redis, a real agent socket. The background reaper is off under test settings, so
each test calls the sweep itself — from a FRESH ``ModelPersistBackend()`` every time, because the
backend is stateless: whichever process sweeps must reach the same verdict.
"""

import asyncio
from datetime import timedelta

import pytest
from django.utils import timezone

from facade import enums, messages
from facade.models import Agent, Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.agent.helpers import open_agent
from tests.factories import build_task

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

DEADLINE = 0.2


def _watchdog(settings, **extra):
    settings.REKUEST_GRACE = {"DEFAULT": 0, "PICKUP_DEADLINE": DEADLINE, **extra}


async def _queued(prefix, agent_pk, *, age=10.0, attempts=1, dispatched=True, **build_kwargs):
    """A task as ``assign`` leaves it — QUEUED, dispatched ``age`` seconds ago, never reported on."""
    task = await build_task(prefix, agent_pk=agent_pk, **build_kwargs)
    then = timezone.now() - timedelta(seconds=age)
    await Task.objects.filter(pk=task.pk).aupdate(
        latest_event_kind=enums.TaskEventKind.QUEUED,
        dispatched_at=then if dispatched else None,
        dispatch_attempts=attempts,
        created_at=then,
    )
    return await Task.objects.aget(pk=task.pk)


async def _kinds(task_id):
    return [e.kind async for e in TaskEvent.objects.filter(task_id=task_id).order_by("id")]


async def _sweep():
    return await ModelPersistBackend().reconcile_unpicked_tasks()


class TestPickupWatchdog:
    async def test_silent_live_agent_gets_one_redelivery_then_is_lost_unstarted(self, settings, agent_ws):
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-silent")
        task = await _queued("wd-silent-t", session.agent_pk)

        assert await _sweep() == 1
        redelivered = await session.receive(messages.Assign)
        assert redelivered.task == str(task.pk)  # the SAME task again, not a new one
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.dispatch_attempts == 2 and refreshed.is_done is False

        assert await _sweep() == 0  # the redelivery restarted the clock
        await asyncio.sleep(DEADLINE * 1.5)
        assert await _sweep() == 1  # still silent → the budget is spent

        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.is_done is True
        assert refreshed.latest_event_kind == enums.TaskEventKind.LOST
        assert await _kinds(task.pk) == [enums.TaskEventKind.QUEUED, enums.TaskEventKind.LOST]
        # Never picked up: nothing ran, so whoever decides may safely send it again.
        assert (await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.LOST)).value["started"] is False

    async def test_any_report_counts_as_picked_up(self, settings, agent_ws):
        """Progress never moves ``latest_event_kind`` off QUEUED — the watchdog must not care."""
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-progress")
        task = await _queued("wd-progress-t", session.agent_pk)

        await ModelPersistBackend().on_agent_progress(session.agent_pk, messages.Progress(task=str(task.pk), progress=0))

        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.picked_up_at is not None
        assert refreshed.latest_event_kind == enums.TaskEventKind.QUEUED  # …which is exactly the trap
        assert await _sweep() == 0
        assert (await Task.objects.aget(pk=task.pk)).is_done is False

    async def test_irreversible_work_that_never_started_is_redelivered_too(self, settings, agent_ws):
        """An agent reports STARTED before it does anything, and drops an Assign for a task it
        already runs: a task that never reported STARTED is safe to send again, whatever its
        effects. (They are information for whoever decides, not the server's rule.)"""
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-phys")
        task = await _queued("wd-phys-t", session.agent_pk, effects="IRREVERSIBLE")

        assert await _sweep() == 1

        assert (await session.receive(messages.Assign)).task == str(task.pk)
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.is_done is False and refreshed.dispatch_attempts == 2

    async def test_physical_work_that_never_left_is_dispatched(self, settings, agent_ws):
        """``dispatched_at`` NULL proves the agent cannot have it — safe to send, even physical."""
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-phys-null")
        task = await _queued("wd-phys-null-t", session.agent_pk, effects="IRREVERSIBLE", dispatched=False)

        assert await _sweep() == 1
        assert (await session.receive(messages.Assign)).task == str(task.pk)
        assert (await Task.objects.aget(pk=task.pk)).is_done is False

    async def test_push_that_failed_after_commit_is_retried(self, settings, agent_ws):
        """The row committed, the transport did not take it (redis down): the row must not rot."""
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-null")
        task = await _queued("wd-null-t", session.agent_pk, dispatched=False)

        assert await _sweep() == 1
        assert (await session.receive(messages.Assign)).task == str(task.pk)
        assert (await Task.objects.aget(pk=task.pk)).dispatched_at is not None

    async def test_offline_agent_is_left_to_the_disconnect_path(self, settings):
        _watchdog(settings)
        seed = await build_task("wd-offline-seed")
        await Agent.objects.filter(pk=seed.agent_id).aupdate(connected=False, last_seen=timezone.now())
        task = await _queued("wd-offline-t", seed.agent_id)

        assert await _sweep() == 0
        assert (await Task.objects.aget(pk=task.pk)).is_done is False

    async def test_cancel_of_unpicked_work_is_honoured(self, settings, agent_ws):
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-cancel")
        task = await _queued("wd-cancel-t", session.agent_pk)
        await Task.objects.filter(pk=task.pk).aupdate(latest_instruct_kind=enums.TaskInstructKind.CANCEL)

        assert await _sweep() == 1

        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.is_done is True and refreshed.latest_event_kind == enums.TaskEventKind.CANCELLED

    async def test_concurrent_backends_redeliver_once(self, settings, agent_ws):
        _watchdog(settings)
        session = await open_agent(agent_ws, "wd-race")
        task = await _queued("wd-race-t", session.agent_pk)

        results = await asyncio.gather(*(ModelPersistBackend().reconcile_unpicked_tasks() for _ in range(4)))

        assert sum(results) == 1
        assert (await Task.objects.aget(pk=task.pk)).dispatch_attempts == 2
        assert (await _kinds(task.pk)).count(enums.TaskEventKind.QUEUED) == 1

    async def test_disabled_by_default(self, settings, agent_ws):
        settings.REKUEST_GRACE = {"DEFAULT": 0}
        session = await open_agent(agent_ws, "wd-off")
        await _queued("wd-off-t", session.agent_pk)
        assert await _sweep() == 0


class TestReconnectDoesNotKillUndeliveredWork:
    async def test_unpicked_work_is_not_inquired_and_its_clock_restarts(self, settings):
        """An Assign still waiting in redis is not "in flight": asking the agent about it gets
        ``Critical: no longer managed`` for a task it is about to receive."""
        _watchdog(settings, DEFAULT=30)  # a reconnect within the grace window: nothing was lost
        running = await build_task("rc-running")
        waiting = await _queued("rc-waiting", running.agent_id)
        backend = ModelPersistBackend()
        agent_id = str(running.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        claim = await ModelPersistBackend().on_agent_connected(agent_id, "c2", session_id="S1")

        inquired = {str(t.pk) for t in claim.tasks}
        assert str(running.pk) in inquired
        assert str(waiting.pk) not in inquired
        # A backlog that built up while it was away must not be redelivered the instant it returns.
        assert await _sweep() == 0

    async def test_fresh_session_does_not_lose_undelivered_work(self, settings):
        _watchdog(settings)
        seed = await build_task("rc-fresh-seed")
        waiting = await _queued("rc-fresh-waiting", seed.agent_id)
        backend = ModelPersistBackend()
        agent_id = str(seed.agent_id)

        await backend.on_agent_connected(agent_id, "c1", session_id="S1")
        await backend.on_agent_disconnected(agent_id, "c1")
        await backend.on_agent_connected(agent_id, "c2", session_id="S2")

        assert enums.TaskEventKind.LOST in await _kinds(seed.pk)  # it WAS running → lost
        assert await _kinds(waiting.pk) == []  # it never ran → simply still queued
        assert (await Task.objects.aget(pk=waiting.pk)).latest_event_kind == enums.TaskEventKind.QUEUED


class TestExpiry:
    async def test_a_lost_agent_now_ends_its_work_lost_at_once(self, settings):
        task = await build_task("exp-now")
        await Agent.objects.filter(pk=task.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(hours=1))
        await ModelPersistBackend().reconcile_orphaned_executor_work(task.agent_id)
        refreshed = await Task.objects.aget(pk=task.pk)
        assert (refreshed.latest_event_kind, refreshed.is_done) == (enums.TaskEventKind.LOST, True)

    async def test_a_late_terminal_report_after_lost_is_kept_beside_it(self, settings):
        """LOST is final: whoever called may already have acted on it."""
        task = await build_task("exp-after-lost")
        await Agent.objects.filter(pk=task.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(hours=1))
        await ModelPersistBackend().reconcile_orphaned_executor_work(task.agent_id)

        await ModelPersistBackend().on_agent_yield(task.agent_id, messages.Yield(task=str(task.pk), returns={"x": 1}))
        await ModelPersistBackend().on_agent_done(task.agent_id, messages.Completed(task=str(task.pk)))

        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.latest_event_kind == enums.TaskEventKind.LOST
        late = [e async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.LATE_REPORT).order_by("id")]
        assert [e.returns for e in late] == [{"x": 1}, None], "the late result is kept too"
        assert late[1].value == {"kind": "COMPLETED"}

    async def test_undelivered_work_of_a_gone_agent_ends_lost_unstarted(self, settings):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "DISCONNECTED_EXPIRY": 0.2}
        seed = await build_task("exp-gone-seed")
        await Agent.objects.filter(pk=seed.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(hours=1))
        waiting = await _queued("exp-gone-t", seed.agent_id)

        assert await ModelPersistBackend().expire_disconnected_tasks() == 1

        refreshed = await Task.objects.aget(pk=waiting.pk)
        assert refreshed.is_done is True and refreshed.latest_event_kind == enums.TaskEventKind.LOST
        assert (await TaskEvent.objects.aget(task_id=waiting.pk, kind=enums.TaskEventKind.LOST)).value["started"] is False


class TestTerminalReportsAreExactlyOnce:
    async def test_the_same_report_on_two_backends_yields_one_event(self):
        """The agent retries an unacked terminal report over its next connection — which may be
        a different backend, while the first is still persisting it."""
        task = await build_task("once")
        report = messages.Completed(task=str(task.pk))

        await asyncio.gather(*(ModelPersistBackend().on_agent_done(task.agent_id, report) for _ in range(4)))

        assert (await _kinds(task.pk)).count(enums.TaskEventKind.COMPLETED) == 1

    async def test_a_sweep_and_a_report_do_not_both_win(self, settings):
        _watchdog(settings)
        task = await build_task("once-race")
        await Agent.objects.filter(pk=task.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(hours=1))

        await asyncio.gather(
            ModelPersistBackend().reconcile_orphaned_executor_work(task.agent_id),
            ModelPersistBackend().on_agent_done(task.agent_id, messages.Completed(task=str(task.pk))),
        )

        kinds = await _kinds(task.pk)
        terminal = [k for k in kinds if k in (enums.TaskEventKind.COMPLETED, enums.TaskEventKind.LOST)]
        assert len(terminal) == 1


class TestWebhookAgentsAreCovered:
    """A HookAgent is always "available" (no socket to be live), and a failed POST used to be
    logged and forgotten — with both sweeps filtering on WEBSOCKET, nothing ever failed the task."""

    async def test_unreachable_hook_is_retried_once_then_lost(self, settings):
        from tests.factories import build_webhook_agent

        _watchdog(settings)
        # A real, closed local port: the POST genuinely fails (connection refused). No mock.
        hook = await build_webhook_agent("wd-hook", hook_url="http://127.0.0.1:9/in")
        task = await _queued("wd-hook-t", hook.pk, dispatched=False, attempts=1)

        assert await _sweep() == 1  # the redelivery — which fails too
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.dispatch_attempts == 2 and refreshed.dispatched_at is None and refreshed.is_done is False

        assert await _sweep() == 1  # budget spent
        refreshed = await Task.objects.aget(pk=task.pk)
        assert refreshed.is_done is True and refreshed.latest_event_kind == enums.TaskEventKind.LOST


class TestReaperPass:
    """``facade.reaper.run_sweeps`` is the whole reconciler — what every backend runs each tick
    (and, on its first tick after boot, what heals everything a previous process left behind;
    there is no management command)."""

    async def test_one_pass_heals_a_crashed_backends_leftovers(self, settings):
        from facade.reaper import run_sweeps

        settings.REKUEST_GRACE = {"DEFAULT": 0.05, "PICKUP_DEADLINE": DEADLINE, "DISCONNECTED_EXPIRY": 3600}
        # A backend died: one agent is stuck ``connected=True`` with a long-expired lease…
        stuck = await build_task("reap-stuck", effects="IRREVERSIBLE")
        await Agent.objects.filter(pk=stuck.agent_id).aupdate(connected=True, last_seen=timezone.now() - timedelta(hours=1))
        # …and another disconnected cleanly, but the process holding its grace window is gone.
        graced = await build_task("reap-graced")
        await Agent.objects.filter(pk=graced.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        await run_sweeps()

        stuck = await Task.objects.aget(pk=stuck.pk)
        assert stuck.is_done is True and stuck.latest_event_kind == enums.TaskEventKind.LOST
        assert (await Agent.objects.aget(pk=stuck.agent_id)).connected is False
        assert (await Task.objects.aget(pk=graced.pk)).latest_event_kind == enums.TaskEventKind.LOST

        await run_sweeps()  # idempotent: a second backend's pass changes nothing
        assert (await _kinds(graced.pk)).count(enums.TaskEventKind.LOST) == 1

    async def test_a_failing_sweep_does_not_starve_the_others(self, settings, monkeypatch):
        import facade.reaper as reaper

        settings.REKUEST_GRACE = {"DEFAULT": 0.05}
        graced = await build_task("reap-isolated")
        await Agent.objects.filter(pk=graced.agent_id).aupdate(connected=False, last_seen=timezone.now() - timedelta(minutes=5))

        async def broken() -> int:
            raise RuntimeError("this sweep is broken")

        real = reaper._sweeps()
        monkeypatch.setattr(reaper, "_sweeps", lambda: [("broken", broken), *real])

        await reaper.run_sweeps()

        assert (await Task.objects.aget(pk=graced.pk)).latest_event_kind == enums.TaskEventKind.LOST
