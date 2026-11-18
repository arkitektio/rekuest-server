"""A patch is one update of a state: a list of operations, not one.

A state changes by updates. Each takes it from one revision to the next, and its operations
(``ops``, RFC 6902) are applied in order and together. Until now a row held one operation
(``op``, ``path``, ``value``), and an update that took several was several rows and several
revisions, which whoever followed the state saw one at a time.

Every row recorded so far becomes an update of its one operation. ``old_value`` goes with
the rest: no agent ever said what a value was before.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0021_command_outbox"),
    ]

    operations = [
        migrations.AddField(
            model_name="patch",
            name="ops",
            field=models.JSONField(
                help_text="The update's operations, as RFC 6902 objects (op, path, value), applied in order and together.",
                null=True,
            ),
        ),
        migrations.RunSQL(
            sql="""
                UPDATE facade_patch
                   SET ops = jsonb_build_array(
                         CASE WHEN op = 'remove'
                              THEN jsonb_build_object('op', op, 'path', path)
                              ELSE jsonb_build_object('op', op, 'path', path, 'value', value)
                         END)
                 WHERE ops IS NULL
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="patch",
            name="ops",
            field=models.JSONField(
                help_text="The update's operations, as RFC 6902 objects (op, path, value), applied in order and together.",
            ),
        ),
        migrations.RemoveField(model_name="patch", name="op"),
        migrations.RemoveField(model_name="patch", name="path"),
        migrations.RemoveField(model_name="patch", name="value"),
        migrations.RemoveField(model_name="patch", name="old_value"),
    ]
