from django.db import migrations


def _on_delete(table: str, column: str, parent: str, action: str) -> str:
    """Recreate ``table.column``'s foreign key with ``ON DELETE <action>``: takt writes and
    deletes these rows in SQL, where Django's own ``on_delete`` never runs."""
    return f"""
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = '{table}'::regclass AND c.contype = 'f' AND a.attname = '{column}';
    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', name);
    ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_ondelete
        FOREIGN KEY ({column}) REFERENCES {parent} (id) ON DELETE {action} DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    """The caller-session keys act in the database. Apart from 0028, which creates them: Django
    adds a new table's foreign keys only at the end of its migration."""

    dependencies = [
        ("facade", "0028_session_protocol"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_on_delete(
                "facade_task", "deadman_session_id", "facade_callersession", "SET NULL"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=_on_delete(
                "facade_callersession", "caller_id", "facade_caller", "CASCADE"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
