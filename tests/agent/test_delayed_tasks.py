"""Delayed tasks (``Task.not_before``): persisted now, handed over by the reaper once due.

Real postgres + redis, a real agent socket. The background reaper is off under test settings, so
each test drives ``dispatch_due_tasks`` itself — from a FRESH ``ModelPersistBackend()`` every
time, because whichever backend sweeps must reach the same verdict.
"""

import threading
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection
from django.utils import timezone

from facade import enums, inputs, messages
from facade.backend import controll_backend
from facade.caller_context import CallerContext
from facade.models import Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.agent.helpers import open_agent
from tests.factories import build_implementation_for_agent

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

DEADLINE = 0.2


async def _assign_delayed(session, impl, *, in_seconds: float, reference: str | None = None) -> Task:
    ctx = CallerContext.from_agent(session.agent)
    return await sync_to_async(controll_backend.assign)(
        ctx,
        inputs.AssignInputModel(
            agent=str(session.agent.pk),
            interface=impl.interface,
            args={},
            reference=reference,
            not_before=timezone.now() + timedelta(seconds=in_seconds),
        ),
    )


async def _make_due(task: Task) -> None:
    await Task.objects.filter(pk=task.pk).aupdate(not_before=timezone.now() - timedelta(seconds=1))


async def _sweep() -> int:
    return await ModelPersistBackend().dispatch_due_tasks()


class TestDelayedTasks:
    async def test_is_held_back_until_due_then_dispatched_once(self, agent_ws):
        session = await open_agent(agent_ws, "delay-basic")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-basic")

        task = await _assign_delayed(session, impl, in_seconds=3600)
        stored = await Task.objects.aget(pk=task.pk)
        assert stored.not_before is not None
        assert stored.dispatched_at is None and stored.dispatch_attempts == 0
        assert await _sweep() == 0  # not due: nothing leaves

        await _make_due(task)
        assert await _sweep() == 1
        assign = await session.receive(messages.Assign)
        assert assign.task == str(task.pk)
        assert assign.interface == impl.interface

        stored = await Task.objects.aget(pk=task.pk)
        assert stored.dispatched_at is not None and stored.dispatch_attempts == 1
        assert await _sweep() == 0  # an ordinary dispatched task now — never sent twice

    async def test_past_not_before_dispatches_immediately(self, agent_ws):
        session = await open_agent(agent_ws, "delay-past")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-past")

        task = await _assign_delayed(session, impl, in_seconds=-60)
        assert (await session.receive(messages.Assign)).task == str(task.pk)
        stored = await Task.objects.aget(pk=task.pk)
        assert stored.not_before is None and stored.dispatch_attempts == 1

    async def test_cancel_before_due_settles_without_an_agent(self, agent_ws):
        session = await open_agent(agent_ws, "delay-cancel")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-cancel")
        task = await _assign_delayed(session, impl, in_seconds=3600)

        await sync_to_async(controll_backend.cancel)(inputs.CancelInputModel(task=str(task.pk)))

        stored = await Task.objects.aget(pk=task.pk)
        assert stored.is_done is True
        assert stored.latest_event_kind == enums.TaskEventKind.CANCELLED
        assert stored.interrupt_at is None  # no control deadline armed: nobody has to confirm
        await _make_due(task)
        assert await _sweep() == 0

    async def test_watchdog_and_expiry_step_over_a_waiting_task(self, settings, agent_ws):
        settings.REKUEST_GRACE = {"DEFAULT": 0, "PICKUP_DEADLINE": DEADLINE, "DISCONNECTED_EXPIRY": DEADLINE}
        session = await open_agent(agent_ws, "delay-watchdog")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-watchdog")
        task = await _assign_delayed(session, impl, in_seconds=3600)
        # Created long ago: every created_at-fallback deadline would already have fired.
        await Task.objects.filter(pk=task.pk).aupdate(created_at=timezone.now() - timedelta(hours=2))

        backend = ModelPersistBackend()
        assert await backend.reconcile_unpicked_tasks() == 0
        assert await backend.expire_disconnected_tasks() == 0
        assert (await Task.objects.aget(pk=task.pk)).is_done is False

    async def test_hooks_are_refused_on_a_delayed_task(self, agent_ws):
        session = await open_agent(agent_ws, "delay-hooks")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-hooks")
        ctx = CallerContext.from_agent(session.agent)

        with pytest.raises(ValueError, match="cannot carry hooks"):
            await sync_to_async(controll_backend.assign)(
                ctx,
                inputs.AssignInputModel(
                    agent=str(session.agent.pk),
                    interface=impl.interface,
                    args={},
                    hooks=[inputs.HookInputModel(kind=enums.HookKind.INIT, hash="x")],
                    not_before=timezone.now() + timedelta(hours=1),
                ),
            )

    async def test_two_backends_racing_dispatch_it_once(self, agent_ws):
        """Real threads, each on its own DB connection: the row lock lets exactly one through."""
        session = await open_agent(agent_ws, "delay-race")
        impl = await build_implementation_for_agent(session.agent.pk, "delay-race")
        task = await _assign_delayed(session, impl, in_seconds=3600)
        await _make_due(task)

        results: list[int] = []
        barrier = threading.Barrier(2)

        def run() -> None:
            try:
                barrier.wait()
                results.append(async_to_sync(ModelPersistBackend().dispatch_due_tasks)())
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(results) == [0, 1]
        assert (await session.receive(messages.Assign)).task == str(task.pk)
        stored = await Task.objects.aget(pk=task.pk)
        assert stored.dispatch_attempts == 1
        assert await TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.CRITICAL).acount() == 0
