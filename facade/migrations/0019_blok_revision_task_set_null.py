"""A blok revision lets go of its task in the database, not only in Django.

takt deletes tasks with its own SQL, and a takt older than 0018 does not know revisions point at
them: run on this schema (a rollback), it would fail to delete any task a revision names. With
``ON DELETE SET NULL`` on the constraint itself, every takt deletes such a task and the revision
keeps its content. A separate migration: Django adds 0018's foreign keys only as that one ends.
"""

from django.db import migrations

SET_NULL = """
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = 'facade_blokrevision'::regclass AND c.contype = 'f' AND a.attname = 'task_id';
    EXECUTE format('ALTER TABLE facade_blokrevision DROP CONSTRAINT %I', name);
    ALTER TABLE facade_blokrevision ADD CONSTRAINT facade_blokrevision_task_id_fk_facade_task_id
        FOREIGN KEY (task_id) REFERENCES facade_task (id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0018_blok_revisions"),
    ]

    operations = [
        migrations.RunSQL(SET_NULL, migrations.RunSQL.noop),
    ]
