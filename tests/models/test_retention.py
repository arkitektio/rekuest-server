"""The task retention sweep: terminal trees past the horizon are deleted, live trees kept."""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from facade import enums
from facade.models import Lock, Task, TaskEvent
from facade.retention import sweep_terminal_tasks

from tests.factories import _build_task


def _finish(task, *, days_ago=0):
    task.is_done = True
    task.latest_event_kind = enums.TaskEventKind.COMPLETED
    task.finished_at = timezone.now() - timedelta(days=days_ago)
    task.save()
    return task


def _tree(prefix, *, days_ago):
    """A root with a child (root_id set, as the fixed assign path now guarantees)."""
    root = _build_task(prefix)
    child = Task.objects.create(
        caller=root.caller,
        action=root.action,
        agent=root.agent,
        implementation=root.implementation,
        parent=root,
        root=root,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )
    TaskEvent.objects.create(task=root, kind=enums.TaskEventChoices.STARTED)
    TaskEvent.objects.create(task=child, kind=enums.TaskEventChoices.STARTED)
    _finish(root, days_ago=days_ago)
    return root, child


@pytest.mark.django_db
class TestRetentionSweep:
    def test_disabled_by_default(self):
        root, child = _tree("ret-off", days_ago=90)
        _finish(child, days_ago=90)
        assert sweep_terminal_tasks() == 0
        assert Task.objects.filter(pk=root.pk).exists()

    @override_settings(TASK_RETENTION_SECONDS=0, EPHEMERAL_TASK_RETENTION_SECONDS=24 * 3600)
    def test_ephemeral_runs_have_their_own_horizon_even_while_retention_is_off(self):
        old_run, old_child = _tree("ret-eph-old", days_ago=2)
        _finish(old_child, days_ago=2)
        Task.objects.filter(pk=old_run.pk).update(ephemeral=True)
        fresh_run = _finish(_build_task("ret-eph-new"), days_ago=0)
        Task.objects.filter(pk=fresh_run.pk).update(ephemeral=True)
        kept, kept_child = _tree("ret-eph-kept", days_ago=2)  # not ephemeral: retention is off
        _finish(kept_child, days_ago=2)

        assert sweep_terminal_tasks() == 1
        assert not Task.objects.filter(pk=old_run.pk).exists()
        assert Task.objects.filter(pk__in=[fresh_run.pk, kept.pk]).count() == 2

    @override_settings(TASK_RETENTION_SECONDS=30 * 24 * 3600)
    def test_old_terminal_tree_is_deleted_with_events(self):
        root, child = _tree("ret-old", days_ago=90)
        _finish(child, days_ago=90)
        recent_root, recent_child = _tree("ret-new", days_ago=1)
        _finish(recent_child, days_ago=1)

        assert sweep_terminal_tasks() == 1
        assert not Task.objects.filter(pk__in=[root.pk, child.pk]).exists()
        assert not TaskEvent.objects.filter(task_id__in=[root.pk, child.pk]).exists()
        # the recent tree is untouched
        assert Task.objects.filter(pk=recent_root.pk).exists()

    @override_settings(TASK_RETENTION_SECONDS=30 * 24 * 3600)
    def test_tree_with_live_descendant_is_kept(self):
        root, child = _tree("ret-live", days_ago=90)  # child still is_done=False
        assert sweep_terminal_tasks() == 0
        assert Task.objects.filter(pk=root.pk).exists()
        assert Task.objects.filter(pk=child.pk).exists()

    @override_settings(TASK_RETENTION_SECONDS=30 * 24 * 3600)
    def test_batching_drains_in_chunks(self):
        for index in range(3):
            root, child = _tree(f"ret-batch-{index}", days_ago=90)
            _finish(child, days_ago=90)
        assert sweep_terminal_tasks(batch_size=1, max_batches=2) == 2
        assert sweep_terminal_tasks(batch_size=1) == 1

    @override_settings(TASK_RETENTION_SECONDS=30 * 24 * 3600)
    def test_lock_holder_is_nulled_not_blocking(self):
        root, child = _tree("ret-lock", days_ago=90)
        _finish(child, days_ago=90)
        lock = Lock.objects.create(agent=root.agent, key="shared", hold_by=root)

        assert sweep_terminal_tasks() == 1
        lock.refresh_from_db()
        assert lock.hold_by is None
