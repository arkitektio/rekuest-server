"""`facade_task (implementation, -created_at)`, in place of the foreign key's own index.

The task table is written to constantly, so the new index is built concurrently (which is
why this migration is not atomic) and only then is the index it replaces dropped: every
lookup by implementation has an index throughout.
"""

import django.db.models.deletion
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("facade", "0004_asset_source"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="task",
            index=models.Index(fields=["implementation", "-created_at"], name="task_impl_created_idx"),
        ),
        migrations.AlterField(
            model_name="task",
            name="implementation",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                help_text="Which implementation is the task currently mapped (can be reassigned)?",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks",
                to="facade.implementation",
            ),
        ),
    ]
