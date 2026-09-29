"""A workflow whose agent dies is resumed, not lost.

It is sent again with its journal (``Assign.resume``): the effects it recorded, by key, and
the last step it took. Its calls find their children again by call key. It is resumed only by
the code it ran, and a call key that names another call is refused as nondeterministic.
"""

import asyncio

import pytest

from facade import enums, messages
from facade.models import Implementation, Task, TaskEvent
from facade.persist_backend import ModelPersistBackend

from tests.agent.helpers import open_agent
from tests.factories import build_implementation_for_agent, build_task

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


@pytest.fixture
def broadcasts(monkeypatch):
    from facade.consumers.async_consumer import AgentConsumer

    recorded = []
    monkeypatch.setattr(AgentConsumer, "broadcast", staticmethod(lambda agent_id, message: recorded.append((agent_id, message))))
    return recorded


async def _workflow(prefix: str, *, code_hash: str = "h1", current: str = "h1") -> Task:
    """A running workflow task: it started, took the clock, and made one call."""
    task = await build_task(prefix)
    await Implementation.objects.filter(pk=task.implementation_id).aupdate(execution=enums.ExecutionChoices.WORKFLOW.value, code_hash=current)
    await Task.objects.filter(pk=task.pk).aupdate(code_hash=code_hash)
    await TaskEvent.objects.acreate(task_id=task.pk, kind=enums.TaskEventKind.STARTED, step=1)
    await TaskEvent.objects.acreate(task_id=task.pk, kind=enums.TaskEventKind.EFFECT, step=2, effect="NOW", key="NOW:1", value=1790000000.5)
    child = await build_task(f"{prefix}-child")
    await Task.objects.filter(pk=child.pk).aupdate(parent_id=task.pk, parent_step=3, call_key="7:3f2b6a9c:1")
    return task


async def _take_over(task: Task) -> None:
    """The agent's process dies and a new one takes the agent over."""
    backend = ModelPersistBackend()
    agent_id = str(task.agent_id)
    await backend.on_agent_connected(agent_id, "c1", session_id="S1")
    await backend.on_agent_disconnected(agent_id, "c1")
    await backend.on_agent_connected(agent_id, "c2", session_id="S2")


async def test_a_workflow_whose_agent_dies_is_sent_again_with_its_journal(settings, broadcasts):
    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30}
    task = await _workflow("wf-resume")

    await _take_over(task)

    refreshed = await Task.objects.aget(pk=task.pk)
    assert (refreshed.latest_event_kind, refreshed.is_done, refreshed.picked_up_at) == (enums.TaskEventKind.QUEUED, False, None)
    ((_, assign),) = broadcasts
    assert isinstance(assign, messages.Assign) and assign.task == str(task.pk)
    assert assign.resume is not None
    assert assign.resume.last_step == 3, "the call's step counts too"
    assert [(e.key, e.effect, e.value) for e in assign.resume.effects] == [("NOW:1", "NOW", 1790000000.5)]


async def test_a_workflow_is_not_resumed_onto_changed_code(settings, broadcasts):
    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30}
    task = await _workflow("wf-changed", code_hash="h1", current="h2")

    await _take_over(task)

    refreshed = await Task.objects.aget(pk=task.pk)
    assert refreshed.latest_event_kind == enums.TaskEventKind.LOST
    lost = await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.LOST)
    assert "code changed" in lost.message
    assert broadcasts == []


async def test_a_resent_workflow_whose_agent_never_comes_back_ends_lost_as_started(settings, broadcasts):
    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30, "DISCONNECTED_EXPIRY": 0.1}
    task = await _workflow("wf-gone")
    await _take_over(task)
    # ...and the new process dies too, before it picks the workflow up.
    await ModelPersistBackend().on_agent_disconnected(str(task.agent_id), "c2")
    await asyncio.sleep(0.2)

    assert await ModelPersistBackend().expire_disconnected_tasks() == 1

    lost = await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.LOST)
    assert lost.value["started"] is True, "it never picked the resend up, but it did start once"


async def test_a_task_that_holds_itself_says_why():
    task = await build_task("wf-hold")

    await ModelPersistBackend().on_agent_paused(
        task.agent_id,
        messages.Paused(task=str(task.pk), message="Check the well", details={"effects": "IRREVERSIBLE", "last_progress": 60}),
    )

    paused = await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.PAUSED)
    assert paused.message == "Check the well"
    assert paused.value == {"effects": "IRREVERSIBLE", "last_progress": 60}
    assert (await Task.objects.aget(pk=task.pk)).latest_event_kind == enums.TaskEventKind.PAUSED


async def test_a_call_key_that_names_another_call_is_refused_as_nondeterministic(agent_ws):
    session = await open_agent(agent_ws, "wf-nondet")
    first_impl = await build_implementation_for_agent(session.agent.pk, "wf-nondet-a")
    other_impl = await build_implementation_for_agent(session.agent.pk, "wf-nondet-b")
    parent = await build_task("wf-nondet-parent")

    await session.send(messages.AssignRequest(call_key="k:1", implementation=str(first_impl.pk), parent=str(parent.pk), args={}))
    assert (await session.receive(messages.AssignResponse)).created is True

    await session.send(messages.AssignRequest(call_key="k:1", implementation=str(other_impl.pk), parent=str(parent.pk), args={}))
    refused = await session.receive(messages.AssignResponse)
    assert refused.task is None and refused.error and refused.error.startswith("Nondeterministic workflow")
    await session.disconnect()


async def test_a_workflow_whose_agent_keeps_dying_is_resumed_only_so_often(settings, broadcasts):
    from facade.persist.reconcile import MAX_RESUMES

    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30}
    task = await _workflow("wf-cap")
    await Task.objects.filter(pk=task.pk).aupdate(resumes=MAX_RESUMES)

    await _take_over(task)

    lost = await TaskEvent.objects.aget(task_id=task.pk, kind=enums.TaskEventKind.LOST)
    assert f"Resumed {MAX_RESUMES} times" in lost.message
    assert broadcasts == []


async def test_each_resume_is_counted(settings, broadcasts):
    settings.REKUEST_GRACE = {**settings.REKUEST_GRACE, "DEFAULT": 30}
    task = await _workflow("wf-count")

    await _take_over(task)

    assert (await Task.objects.aget(pk=task.pk)).resumes == 1


async def test_a_late_resend_of_an_effect_does_not_add_a_second_value():
    """The first value under a key stands: it is the one a resumed run replays."""
    task = await build_task("wf-effect-once")
    backend = ModelPersistBackend()

    await backend.on_agent_effect(task.agent_id, messages.Effect(task=str(task.pk), effect="NOW", value=1.0, key="NOW:1"))
    await backend.on_agent_effect(task.agent_id, messages.Effect(task=str(task.pk), effect="NOW", value=2.0, key="NOW:1"))

    effects = [e async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.EFFECT)]
    assert [(e.key, e.value) for e in effects] == [("NOW:1", 1.0)]
