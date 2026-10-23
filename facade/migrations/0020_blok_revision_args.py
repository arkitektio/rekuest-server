"""A blok revision holds the blok's args, so an edit can change them and an undo brings them back.

Until now a custom blok's args never changed after it was made (only a fork set them), so every
revision written so far had the args its blok has now: they are copied onto it. Its
``content_hash`` stays as written, which counts no args: the first write to such a blok commits a
revision even when nothing else changed.
"""

from django.db import migrations, models

BACKFILL = """
UPDATE facade_blokrevision r
   SET args = b.args
  FROM facade_blok b
 WHERE r.blok_id = b.id AND b.args <> '[]'::jsonb;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0019_blok_revision_task_set_null"),
    ]

    operations = [
        migrations.AddField(
            model_name="blokrevision",
            name="args",
            field=models.JSONField(db_default=[], default=list, help_text="The args of this revision (rekuest_core ArgPortInput): the items the blok is opened for."),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
    ]
