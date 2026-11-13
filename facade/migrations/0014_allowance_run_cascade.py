"""A run's allowances go with it in the database, not only in Django: takt deletes tasks with
its own SQL (and an older takt, after a rollback, does not know the table at all).

Hand-written: ``makemigrations`` emits none of this, so carry it whenever the history is
regenerated. In its own migration because Django adds 0013's foreign keys only as that one ends.
"""

from django.db import migrations

CASCADE = """
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = 'facade_actionallowance'::regclass AND c.contype = 'f' AND a.attname = 'run_id';
    EXECUTE format('ALTER TABLE facade_actionallowance DROP CONSTRAINT %I', name);
    ALTER TABLE facade_actionallowance ADD CONSTRAINT facade_actionallowance_run_id_fk_facade_task_id
        FOREIGN KEY (run_id) REFERENCES facade_task (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0013_action_allowance"),
    ]

    operations = [
        migrations.RunSQL(CASCADE, migrations.RunSQL.noop),
    ]
