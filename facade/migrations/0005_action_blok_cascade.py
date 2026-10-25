"""An action's own bloks go with their action in the database, not only in Django.

takt deletes actions with its own SQL. The takt of this release deletes their ``facade_actionblok``
rows first; an older one (a failed update, a rollback) does not know the table, so ``ON DELETE
CASCADE`` sits on the constraint itself and its delete still goes through. The bloks it leaves
without an action are collected by the next registration of this release that held them.

Hand-written, so re-add it whenever the history is regenerated: ``makemigrations`` emits none of
this. In its own migration because Django adds 0004's foreign keys only as that one ends.
"""

from django.db import migrations

CASCADE = """
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = 'facade_actionblok'::regclass AND c.contype = 'f' AND a.attname = 'action_id';
    EXECUTE format('ALTER TABLE facade_actionblok DROP CONSTRAINT %I', name);
    ALTER TABLE facade_actionblok ADD CONSTRAINT facade_actionblok_action_id_fk_facade_action_id
        FOREIGN KEY (action_id) REFERENCES facade_action (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0004_action_bloks"),
    ]

    operations = [
        migrations.RunSQL(CASCADE, migrations.RunSQL.noop),
    ]
