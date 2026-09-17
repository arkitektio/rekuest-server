from django.db import migrations, models


def dedupe_task_references(apps, schema_editor):
    """Make ``(caller, reference)`` unique — by RENAMING the losers, never deleting them.

    A duplicate is a task that really ran: deleting it would cascade to its events, instructs,
    children and patches. The oldest row keeps the reference (it is the one a retry of that
    assign is meant to find); every later row gets a suffix that cannot collide.

    The table lock closes the window between this dedupe and the constraint below: a replica
    still running the previous release could otherwise insert a fresh duplicate in between and
    fail the migration. ``SHARE ROW EXCLUSIVE`` blocks writers, not readers, and is released
    when the migration's transaction commits.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("LOCK TABLE facade_task IN SHARE ROW EXCLUSIVE MODE")
    schema_editor.execute(
        """
        UPDATE facade_task AS t
        SET reference = left(t.reference, 960) || '~dup-' || t.id
        FROM (
            SELECT id, row_number() OVER (PARTITION BY caller_id, reference ORDER BY id) AS position
            FROM facade_task
            WHERE caller_id IS NOT NULL
        ) AS ranked
        WHERE ranked.id = t.id AND ranked.position > 1
        """
    )
    # Django's FKs are DEFERRABLE INITIALLY DEFERRED, so the UPDATE above leaves pending trigger
    # events and Postgres would refuse the AddConstraint that follows in this same transaction.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0006_task_deadlines"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="revision",
            field=models.PositiveBigIntegerField(
                db_default=1,
                default=1,
                help_text="Monotonic per-task version, bumped by every write to this row (see ``save``). Carried in the task change feeds so a consumer can discard an update that arrives out of order — with several backends writing, channel-layer arrival order is not commit order.",
            ),
        ),
        migrations.RunPython(dedupe_task_references, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.UniqueConstraint(fields=("caller", "reference"), name="task_unique_reference_per_caller"),
        ),
        # The constraint's own index now serves the dedupe lookup.
        migrations.RemoveIndex(
            model_name="task",
            name="task_caller_ref_idx",
        ),
    ]
