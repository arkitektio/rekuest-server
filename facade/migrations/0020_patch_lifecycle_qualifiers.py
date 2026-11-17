"""A patch says its lifecycle and its qualifiers.

``lifecycle`` is said by the agent's runtime when a change is not made in the ordinary
course of a task (``cleanup``, ``shutdown``); ``qualifiers`` are the app's own names for
what a change was for. Both are NULL on every patch recorded before.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0019_module_contexts"),
    ]

    operations = [
        migrations.AddField(
            model_name="patch",
            name="lifecycle",
            field=models.CharField(
                blank=True,
                help_text="Where in a task's or the agent's life the change was made, when not in its ordinary course: 'cleanup' (a task being stopped, on its way out) or 'shutdown' (the agent's shutdown hooks). Said by the agent's runtime; NULL for an ordinary change.",
                max_length=1000,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="patch",
            name="qualifiers",
            field=models.JSONField(
                blank=True,
                help_text="What the app says the change was for: a list of its own names. NULL when it said nothing.",
                null=True,
            ),
        ),
    ]
