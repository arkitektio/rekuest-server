"""Where the bloks a task showed stand (``Task.bloks``), read off its events.

takt makes them (``takt/crates/facade/tests/holds.rs``); here only the reading: an ask WAITING
until its DECIDED, then ANSWERED with the values or DISMISSED, GONE when the task ended first;
an inform SHOWN. They are never listed.
"""

import pytest

from facade import enums
from facade.models import Blok, MaterializedBlok, TaskEvent, UICatalog
from facade.types.task import task_bloks
from tests.factories import _build_task


def _shown(task, role: str) -> MaterializedBlok:
    organization = task.agent.organization
    blok = Blok.objects.create(
        name=f"task-{task.id}-{role}",
        organization=organization,
        creator=task.agent.user,
        catalog=UICatalog.objects.get_or_create(name="orkestrator", organization=organization)[0],
        owner_task=task,
        returns=[{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}],
    )
    return MaterializedBlok.objects.create(blok=blok, name=blok.name, description="", scope="TASK", owner_task=task, task_role=role)


def _event(task, kind: str, value: dict, message: str | None = None) -> None:
    TaskEvent.objects.create(task=task, kind=kind, value=value, message=message)


@pytest.mark.django_db
def test_an_ask_waits_then_is_answered_and_an_inform_is_shown() -> None:
    task = _build_task("ask")
    ask = _shown(task, "ASK")
    _event(task, "HELD", {"kind": "self", "blok": str(ask.id)}, "Which one?")
    inform = _shown(task, "INFORM")
    _event(task, "INFORM", {"blok": str(inform.id)}, "Halfway")

    waiting, shown = task_bloks(task)
    assert (waiting.role, waiting.status, waiting.message) == (enums.TaskBlokRole.ASK, enums.TaskBlokStatus.WAITING, "Which one?")
    assert (shown.role, shown.status, shown.message) == (enums.TaskBlokRole.INFORM, enums.TaskBlokStatus.SHOWN, "Halfway")

    picked = {"image": {"__identifier": "@mikro/image", "object": "8"}}
    _event(task, "DECIDED", {"approved": True, "values": picked, "user": "someone"})
    answered = task_bloks(task)[0]
    assert (answered.status, answered.values, answered.decided_by) == (enums.TaskBlokStatus.ANSWERED, picked, "someone")


@pytest.mark.django_db
def test_an_ask_dismissed_or_left_by_its_task() -> None:
    dismissed = _build_task("dismissed")
    ask = _shown(dismissed, "ASK")
    _event(dismissed, "HELD", {"kind": "self", "blok": str(ask.id)})
    _event(dismissed, "DECIDED", {"approved": False, "user": "someone"})
    assert task_bloks(dismissed)[0].status == enums.TaskBlokStatus.DISMISSED

    left = _build_task("left")
    ask = _shown(left, "ASK")
    _event(left, "HELD", {"kind": "self", "blok": str(ask.id)})
    left.is_done = True
    left.save()
    assert task_bloks(left)[0].status == enums.TaskBlokStatus.GONE


@pytest.mark.django_db
def test_a_task_without_bloks_has_none() -> None:
    assert task_bloks(_build_task("plain")) == []
