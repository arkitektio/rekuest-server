from django.db import migrations


def _cascade(table: str, column: str, parent: str) -> str:
    """Recreate ``table.column``'s foreign key with ``ON DELETE CASCADE``: takt deletes bloks and
    materializations in SQL, which would otherwise fail on a pin (an older takt too)."""
    return f"""
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = '{table}'::regclass AND c.contype = 'f' AND a.attname = '{column}';
    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', name);
    ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_cascade
        FOREIGN KEY ({column}) REFERENCES {parent} (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    """The pin tables' keys cascade. Apart from 0026, which creates them: Django adds a new table's
    foreign keys only at the end of its migration."""

    dependencies = [
        ("facade", "0026_blok_pins"),
    ]

    operations = [
        migrations.RunSQL(_cascade("facade_blok_pinned_by", "blok_id", "facade_blok"), migrations.RunSQL.noop),
        migrations.RunSQL(_cascade("facade_materializedblok_pinned_by", "materializedblok_id", "facade_materializedblok"), migrations.RunSQL.noop),
    ]
