"""Full-stack caller-event return path (item 8).

When an agent is the *caller* of a task, events on that task are streamed
back to its own socket as ``Caller*`` messages. Here the registered agent is both the
executor (it reports a ProgressEvent) and the caller (the task's caller is its own
identity), so the progress it reports comes straight back to it as a ``ProgressEvent``.
"""

import pytest

from facade import messages

from tests.agent.helpers import open_agent
from tests.factories import build_task, build_task_for_agent_caller


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestCallerEventReturn:
    async def test_caller_receives_progress_for_own_task(self, agent_ws):
        session = await open_agent(agent_ws, "callerev-agent")
        task = await build_task_for_agent_caller(session.agent.pk, "callerev")

        # Report progress as the executor …
        await session.send(messages.Progress(task=str(task.pk), progress=42, message="halfway"))

        # … and receive it back as the caller.
        msg = await session.receive(messages.ProgressEvent)
        assert msg.task == str(task.pk)
        assert msg.progress == 42 and msg.message == "halfway"
        assert msg.event and msg.seq  # dedup handle + ordering key present

        await session.disconnect()

    async def test_done_comes_back_as_caller_done(self, agent_ws):
        session = await open_agent(agent_ws, "callerdone-agent")
        task = await build_task_for_agent_caller(session.agent.pk, "callerdone")

        await session.send(messages.Completed(task=str(task.pk)))

        msg = await session.receive(messages.CompletedEvent)
        assert msg.task == str(task.pk)
        await session.disconnect()

    async def test_only_own_caller_tasks_are_delivered(self, agent_ws):
        # Two tasks: one whose caller is us, one whose caller is a different identity.
        # Progress is reported on the OTHER first, then on OURS. Only ours must come back,
        # so the single ProgressEvent we receive must be for our task (progress 77),
        # never the other's (progress 10).
        session = await open_agent(agent_ws, "calleriso-agent")
        mine = await build_task_for_agent_caller(session.agent.pk, "callermine")
        other = await build_task("callerother")  # caller = its own identity, not ours

        await session.send(messages.Progress(task=str(other.pk), progress=10))
        await session.send(messages.Progress(task=str(mine.pk), progress=77))

        msg = await session.receive(messages.ProgressEvent)
        assert msg.task == str(mine.pk) and msg.progress == 77

        await session.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestCallerHearsItsTaskWasLost:
    async def test_a_lost_task_comes_back_as_lost_with_what_is_known(self, agent_ws):
        """The caller stays connected while the agent running its task dies."""
        from asgiref.sync import sync_to_async

        from facade.models import Agent, Implementation, Task
        from facade.persist_backend import ModelPersistBackend
        from tests.factories import _seed_throwaway_agent_graph

        caller = await open_agent(agent_ws, "lost-caller")
        task = await build_task_for_agent_caller(caller.agent.pk, "lost")
        # Hand the execution to another agent, which then dies mid-task.
        executor = await sync_to_async(_seed_throwaway_agent_graph)("lost-executor")
        await Implementation.objects.filter(pk=task.implementation_id).aupdate(agent=executor, effects="IRREVERSIBLE")
        await Task.objects.filter(pk=task.pk).aupdate(agent=executor)
        await ModelPersistBackend().on_agent_progress(executor.pk, messages.Progress(task=str(task.pk), progress=60))
        await Agent.objects.filter(pk=executor.pk).aupdate(connected=False)

        await ModelPersistBackend().reconcile_orphaned_executor_work(executor.pk)

        msg = await caller.receive(messages.LostEvent)
        assert msg.task == str(task.pk)
        assert (msg.started, msg.last_progress, msg.effects) == (True, 60, "IRREVERSIBLE")
        await caller.disconnect()
