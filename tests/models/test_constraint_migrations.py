"""The dedupe steps of migrations 0007/0008, exercised on seeded duplicates.

These functions are no-ops on any database that is already clean — which is every test database,
and which is exactly why they need their own tests. They run once, on a production database, under
a table lock, mid-deploy. A survivor rule that is subtly inverted (keeping the free lock instead of
the held one, deleting a Task instead of renaming it) would be discovered there or not at all.

Each test drops the constraint the migration adds, seeds the duplicates the constraint now forbids,
runs the real migration function, and asserts the survivor rule before putting the constraint back.
``connection.schema_editor()`` is what makes that possible: it provides both the editor the
functions take and the transaction block their ``LOCK TABLE`` requires.
"""

import contextlib
import importlib

import pytest
from django.apps import apps as real_apps
from django.db import connection

from facade import enums
from facade.models import (
    Blok,
    BlokAgentMapping,
    Lock,
    MaterializedBlok,
    MemoryDrawer,
    MemoryShelve,
    Patch,
    Session,
    Snapshot,
    Task,
    UICatalog,
)

from tests.factories import _build_state_for_agent, _build_task, _seed_throwaway_agent_graph

pytestmark = pytest.mark.django_db(transaction=True)

migration_0007 = importlib.import_module("facade.migrations.0007_task_reference_unique_revision")
migration_0008 = importlib.import_module("facade.migrations.0008_multi_replica_constraints")


@contextlib.contextmanager
def without_constraint(model, name):
    """Drop ``name`` for the duration, so the duplicates it forbids can be seeded.

    The ``schema_editor`` is also what the migration functions need: they issue
    ``LOCK TABLE``, which Postgres rejects outside a transaction block.
    """
    constraint = next(c for c in model._meta.constraints if c.name == name)
    with connection.schema_editor(atomic=True) as editor:
        editor.remove_constraint(model, constraint)
        try:
            yield editor
        finally:
            # The seeding above was ORM inserts, which leave deferred FK checks pending; the
            # migrations settle their own, but the test's own writes need it too.
            editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            editor.add_constraint(model, constraint)


def constraint_exists(table: str, name: str) -> bool:
    """Whether the uniqueness is actually enforced on ``table``.

    Two shapes to look for: a plain ``UniqueConstraint`` becomes a real ``pg_constraint`` row,
    while one with a ``condition`` (the partial ones here) can only be a unique *index*.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass", [name, table])
        if cursor.fetchone() is not None:
            return True
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s AND tablename = %s", [name, table])
        return cursor.fetchone() is not None


class TestTaskReferenceDedupe:
    def test_losers_are_renamed_not_deleted(self):
        """Renaming, because a Task is the root of a tree: events, instructs, children and patches
        hang off it, and ``DELETE`` would take a real execution's history with it."""
        keeper = _build_task("dd-task")
        with without_constraint(Task, "task_unique_reference_per_caller") as editor:
            loser = Task.objects.create(
                action=keeper.action,
                agent=keeper.agent,
                implementation=keeper.implementation,
                caller=keeper.caller,
                reference=keeper.reference,
                latest_event_kind=enums.TaskEventKind.STARTED,
                latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
            )

            migration_0007.dedupe_task_references(real_apps, editor)

            assert Task.objects.filter(pk=keeper.pk).exists() and Task.objects.filter(pk=loser.pk).exists()
            # ``reference`` defaults to the ``uuid.uuid4`` callable, so an in-memory instance holds
            # a UUID object where the column holds its string form.
            reference = str(keeper.reference)
            # The OLDEST row keeps the reference: that is the one a retry of the assign must find.
            assert Task.objects.get(pk=keeper.pk).reference == reference
            assert Task.objects.get(pk=loser.pk).reference == f"{reference}~dup-{loser.pk}"

        assert constraint_exists("facade_task", "task_unique_reference_per_caller")

    def test_callerless_rows_are_left_alone(self):
        """``caller`` is nullable and Postgres treats NULLs as distinct, so caller-less tasks are
        not duplicates of each other and must not be renamed."""
        first = _build_task("dd-task-null-a")
        Task.objects.filter(pk=first.pk).update(caller=None, reference="shared")
        second = _build_task("dd-task-null-b")
        Task.objects.filter(pk=second.pk).update(caller=None, reference="shared")

        with without_constraint(Task, "task_unique_reference_per_caller") as editor:
            migration_0007.dedupe_task_references(real_apps, editor)

        assert [t.reference for t in Task.objects.filter(pk__in=[first.pk, second.pk])] == ["shared", "shared"]


class TestLockDedupe:
    def test_the_held_row_survives(self):
        task = _build_task("dd-lock")
        with without_constraint(Lock, "lock_unique_key_per_agent") as editor:
            free = Lock.objects.create(agent=task.agent, key="stage")
            held = Lock.objects.create(agent=task.agent, key="stage", hold_by=task)

            migration_0008.dedupe_locks(real_apps, editor)

            # A lock row only *reports* what the agent's own in-process lock is doing, so the row
            # claiming a holder is the more truthful one.
            assert list(Lock.objects.filter(agent=task.agent, key="stage").values_list("pk", flat=True)) == [held.pk]
            assert not Lock.objects.filter(pk=free.pk).exists()

        assert constraint_exists("facade_lock", "lock_unique_key_per_agent")

    def test_different_keys_and_agents_are_untouched(self):
        task = _build_task("dd-lock-distinct")
        other = _seed_throwaway_agent_graph("dd-lock-other")
        with without_constraint(Lock, "lock_unique_key_per_agent") as editor:
            mine = Lock.objects.create(agent=task.agent, key="stage")
            another_key = Lock.objects.create(agent=task.agent, key="camera")
            another_agent = Lock.objects.create(agent=other, key="stage")

            migration_0008.dedupe_locks(real_apps, editor)

            assert Lock.objects.filter(pk__in=[mine.pk, another_key.pk, another_agent.pk]).count() == 3


class TestSessionDedupe:
    def test_the_log_is_merged_onto_the_oldest_session(self):
        """Two rows for one logical session split its patch/snapshot log, and state reconstruction
        then silently returns half a history — so the losers' rows are re-pointed, not dropped."""
        agent = _seed_throwaway_agent_graph("dd-session")
        state = _build_state_for_agent(agent.pk, "dd-session", "dd-session")

        with without_constraint(Session, "session_unique_id_per_agent") as editor:
            keeper = Session.objects.create(agent=agent, session_id="S1", active=False)
            loser = Session.objects.create(agent=agent, session_id="S1", active=True)
            patch = Patch.objects.create(state=state, agent=agent, interface="dd-session", session=loser, op="replace", path="/x", value=1, global_rev=1)
            snapshot = Snapshot.objects.create(state=state, agent=agent, session=loser, value={}, global_rev=1)

            migration_0008.dedupe_sessions(real_apps, editor)

            assert list(Session.objects.filter(agent=agent).values_list("pk", flat=True)) == [keeper.pk]
            assert Patch.objects.get(pk=patch.pk).session_id == keeper.pk
            assert Snapshot.objects.get(pk=snapshot.pk).session_id == keeper.pk
            # A session any writer still considered live stays live.
            assert Session.objects.get(pk=keeper.pk).active is True

        assert constraint_exists("facade_session", "session_unique_id_per_agent")


class TestMemoryDrawerDedupe:
    def _shelve(self, agent, prefix):
        return MemoryShelve.objects.create(agent=agent, name=prefix, description="", creator=agent.user, organization=agent.organization)

    def test_the_latest_upsert_survives_and_nulls_are_not_duplicates(self):
        agent = _seed_throwaway_agent_graph("dd-drawer")
        shelve = self._shelve(agent, "dd-drawer")

        with without_constraint(MemoryDrawer, "drawer_unique_resource_per_shelve") as editor:
            stale = MemoryDrawer.objects.create(shelve=shelve, resource_id="r-1", identifier="i")
            latest = MemoryDrawer.objects.create(shelve=shelve, resource_id="r-1", identifier="i")
            # ``resource_id`` is nullable; two NULL drawers are not duplicates of each other.
            anonymous_one = MemoryDrawer.objects.create(shelve=shelve, resource_id=None, identifier="i")
            anonymous_two = MemoryDrawer.objects.create(shelve=shelve, resource_id=None, identifier="i")

            migration_0008.dedupe_memory_drawers(real_apps, editor)

            assert list(MemoryDrawer.objects.filter(shelve=shelve, resource_id="r-1").values_list("pk", flat=True)) == [latest.pk]
            assert not MemoryDrawer.objects.filter(pk=stale.pk).exists()
            assert MemoryDrawer.objects.filter(pk__in=[anonymous_one.pk, anonymous_two.pk]).count() == 2

        assert constraint_exists("facade_memorydrawer", "drawer_unique_resource_per_shelve")


class TestDeclaredBlokBackfill:
    def _blok(self, agent, name):
        catalog = UICatalog.objects.create(name=f"{name}-catalog", organization=agent.organization)
        return Blok.objects.create(name=name, organization=agent.organization, description="", creator=agent.user, catalog=catalog)

    def test_a_single_agents_materialization_is_adopted(self):
        agent = _seed_throwaway_agent_graph("dd-blok")
        blok = self._blok(agent, "dd-blok")
        declared = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        BlokAgentMapping.objects.create(materialized_blok=declared, agent=agent, key="general")

        with connection.schema_editor(atomic=True) as editor:
            migration_0008.adopt_declared_bloks(real_apps, editor)

        assert MaterializedBlok.objects.get(pk=declared.pk).declared_by_id == agent.pk

    def test_hand_made_and_ambiguous_rows_stay_user_owned(self):
        """Only a row registration would have produced is adopted: same name as its blok, and
        every mapping bound to one agent. Anything else keeps ``declared_by`` NULL, which is what
        lets a user keep several materializations of one blok."""
        agent = _seed_throwaway_agent_graph("dd-blok-keep")
        other = _seed_throwaway_agent_graph("dd-blok-keep-other")
        blok = self._blok(agent, "dd-blok-keep")

        renamed = MaterializedBlok.objects.create(blok=blok, name="a user's own name", description="")
        BlokAgentMapping.objects.create(materialized_blok=renamed, agent=agent, key="general")
        unmapped = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        spanning = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        BlokAgentMapping.objects.create(materialized_blok=spanning, agent=agent, key="a")
        BlokAgentMapping.objects.create(materialized_blok=spanning, agent=other, key="b")

        with connection.schema_editor(atomic=True) as editor:
            migration_0008.adopt_declared_bloks(real_apps, editor)

        for mblok in (renamed, unmapped, spanning):
            assert MaterializedBlok.objects.get(pk=mblok.pk).declared_by_id is None

    def test_only_one_row_per_agent_is_adopted(self):
        """Two candidate rows for the same (blok, agent) would violate the partial constraint the
        migration adds next, so only the first is claimed."""
        agent = _seed_throwaway_agent_graph("dd-blok-two")
        blok = self._blok(agent, "dd-blok-two")
        first = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        BlokAgentMapping.objects.create(materialized_blok=first, agent=agent, key="general")
        second = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        BlokAgentMapping.objects.create(materialized_blok=second, agent=agent, key="general")

        with connection.schema_editor(atomic=True) as editor:
            migration_0008.adopt_declared_bloks(real_apps, editor)

        assert MaterializedBlok.objects.get(pk=first.pk).declared_by_id == agent.pk
        assert MaterializedBlok.objects.get(pk=second.pk).declared_by_id is None
        assert constraint_exists("facade_materializedblok", "mblok_unique_declaration_per_agent")
