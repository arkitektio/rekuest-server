"""Delayed tasks: ``Task.not_before``.

A task created with a future ``not_before`` is persisted but not dispatched; the reaper's
``dispatch_due_tasks`` hands it over once due. Nullable (every existing task dispatched on
creation), plus a partial index holding only the delayed rows that were never dispatched.
Pure schema change.
"""


from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0010_agent_description'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='not_before',
            field=models.DateTimeField(blank=True, help_text="Hold the Assign back until then. NULL = dispatch on creation. A delayed task stays undispatched (``dispatch_attempts == 0``) until the reaper's ``dispatch_due_tasks`` hands it over; its pickup deadline starts at that dispatch, not at creation.", null=True),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(condition=models.Q(('dispatch_attempts', 0), ('is_done', False), ('not_before__isnull', False)), fields=['not_before'], name='task_not_before_due_idx'),
        ),
    ]
