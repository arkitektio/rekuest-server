"""Uniqueness for the four upserted-but-unconstrained tables, plus ``declared_by``.

Each of these is upserted with ``update_or_create`` / ``get_or_create`` on fields the database
did not constrain. That is safe in one process and unsafe in several: ``select_for_update`` on a
row that does not exist yet locks nothing, so two backends both create — and from then on every
upsert on those fields raises ``MultipleObjectsReturned``, permanently, for that agent.

Each concern is deduped and constrained inside ONE migration (never a data migration followed by
a schema one: the table lock would drop in between, and a failed constraint step would not re-run
the dedupe). ``LOCK TABLE … SHARE ROW EXCLUSIVE`` blocks writers — a replica still running the
previous release cannot insert a fresh duplicate between the dedupe and the constraint — while
leaving readers alone.
"""

import django.db.models.deletion
from django.db import migrations, models


def _lock_table(schema_editor, table: str) -> bool:
    if schema_editor.connection.vendor != "postgresql":
        return False
    schema_editor.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
    return True


def _settle_constraints(schema_editor) -> None:
    """Force deferred FK checks to run now, before the ``ALTER TABLE`` that follows.

    Django declares its foreign keys ``DEFERRABLE INITIALLY DEFERRED``, so any DML in this
    transaction leaves pending trigger events — and Postgres refuses to ``ALTER TABLE`` a table
    that has them ("cannot ALTER TABLE … because it has pending trigger events"). Every dedupe
    here is followed by an ``AddConstraint`` in the same transaction, so every dedupe settles.
    """
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def dedupe_locks(apps, schema_editor):
    """One row per (agent, key). Survivor: the one that looks live.

    A lock row is only a *report* of what the agent's own in-process lock is doing, so the row
    claiming a holder is the more truthful one; ties go to the most recently written. Nothing
    references Lock, so the losers can simply go.
    """
    if not _lock_table(schema_editor, "facade_lock"):
        return
    schema_editor.execute(
        """
        DELETE FROM facade_lock WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY agent_id, key
                    ORDER BY (hold_by_id IS NULL), updated_at DESC, id
                ) AS position
                FROM facade_lock
            ) ranked WHERE position > 1
        )
        """
    )
    _settle_constraints(schema_editor)


def dedupe_sessions(apps, schema_editor):
    """One row per (agent, session_id). Survivor: the lowest id — the session's true start.

    Patches and snapshots of the losing rows are re-pointed onto it first, so the log is merged
    rather than truncated; ``active`` is OR-ed so a session any writer still considered live stays
    live.
    """
    if not _lock_table(schema_editor, "facade_session"):
        return
    schema_editor.execute(
        """
        CREATE TEMPORARY TABLE session_merge ON COMMIT DROP AS
        SELECT s.id AS loser_id, k.keep_id
        FROM facade_session s
        JOIN (
            SELECT agent_id, session_id, min(id) AS keep_id, bool_or(active) AS any_active
            FROM facade_session GROUP BY agent_id, session_id
        ) k ON k.agent_id = s.agent_id AND k.session_id = s.session_id
        WHERE s.id <> k.keep_id
        """
    )
    schema_editor.execute("UPDATE facade_patch SET session_id = m.keep_id FROM session_merge m WHERE facade_patch.session_id = m.loser_id")
    schema_editor.execute("UPDATE facade_snapshot SET session_id = m.keep_id FROM session_merge m WHERE facade_snapshot.session_id = m.loser_id")
    schema_editor.execute(
        """
        UPDATE facade_session SET active = TRUE WHERE id IN (
            SELECT k.keep_id FROM (
                SELECT agent_id, session_id, min(id) AS keep_id, bool_or(active) AS any_active
                FROM facade_session GROUP BY agent_id, session_id
            ) k WHERE k.any_active
        )
        """
    )
    schema_editor.execute("DELETE FROM facade_session WHERE id IN (SELECT loser_id FROM session_merge)")
    _settle_constraints(schema_editor)


def dedupe_memory_drawers(apps, schema_editor):
    """One row per (shelve, resource_id). Survivor: the highest id — the most recent upsert.

    Drawer ids only ever reach a client as something to pass back to ``collect``, where they are
    matched with ``id__in``; a dropped id matches nothing and is harmless. Drawers are a scratch
    store that every registration wipes anyway.
    """
    if not _lock_table(schema_editor, "facade_memorydrawer"):
        return
    schema_editor.execute(
        """
        DELETE FROM facade_memorydrawer WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (PARTITION BY shelve_id, resource_id ORDER BY id DESC) AS position
                FROM facade_memorydrawer WHERE resource_id IS NOT NULL
            ) ranked WHERE position > 1
        )
        """
    )
    _settle_constraints(schema_editor)


def adopt_declared_bloks(apps, schema_editor):
    """Attribute pre-existing auto-materializations to the agent that declared them.

    Never merges rows: ``BlokAgentMapping`` is unique on (materialized_blok, key), so merging two
    materializations would collide on their mappings. A row is recognised as one agent's
    declaration when registration is what would have produced it: its name still matches the
    blok's, and every one of its mappings binds that same agent. Anything else — a hand-made
    materialization, or one whose mappings span agents — stays NULL, i.e. user-owned. A blok
    whose declaration is not recognised simply gets a fresh row on the next registration.
    """
    MaterializedBlok = apps.get_model("facade", "MaterializedBlok")
    BlokAgentMapping = apps.get_model("facade", "BlokAgentMapping")

    claimed: set[tuple[int, int]] = set()
    for mblok in MaterializedBlok.objects.select_related("blok").order_by("id").iterator():
        if mblok.name != mblok.blok.name:
            continue
        agent_ids = set(BlokAgentMapping.objects.filter(materialized_blok=mblok).values_list("agent_id", flat=True))
        if len(agent_ids) != 1:
            continue
        agent_id = agent_ids.pop()
        if agent_id is None or (mblok.blok_id, agent_id) in claimed:
            continue
        claimed.add((mblok.blok_id, agent_id))
        mblok.declared_by_id = agent_id
        mblok.save(update_fields=["declared_by"])
    _settle_constraints(schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0007_task_reference_unique_revision"),
    ]

    operations = [
        migrations.RunPython(dedupe_locks, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="lock",
            constraint=models.UniqueConstraint(fields=("agent", "key"), name="lock_unique_key_per_agent"),
        ),
        migrations.RunPython(dedupe_sessions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="session",
            constraint=models.UniqueConstraint(fields=("agent", "session_id"), name="session_unique_id_per_agent"),
        ),
        migrations.RunPython(dedupe_memory_drawers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="memorydrawer",
            constraint=models.UniqueConstraint(
                condition=models.Q(("resource_id__isnull", False)),
                fields=("shelve", "resource_id"),
                name="drawer_unique_resource_per_shelve",
            ),
        ),
        migrations.AddField(
            model_name="materializedblok",
            name="declared_by",
            field=models.ForeignKey(
                blank=True,
                help_text="The agent whose registration auto-materialized this blok, if any. NULL means a user created it by hand. An auto-materialization is meaningless without its agent, hence CASCADE.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="declared_materialized_bloks",
                to="facade.agent",
            ),
        ),
        migrations.RunPython(adopt_declared_bloks, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="materializedblok",
            constraint=models.UniqueConstraint(
                condition=models.Q(("declared_by__isnull", False)),
                fields=("blok", "declared_by"),
                name="mblok_unique_declaration_per_agent",
            ),
        ),
    ]
