"""A human-readable description for an Agent.

An agent's ``name`` falls back to the client id in both registration paths, so a fleet reads
as a column of near-identical strings. ``description`` is what distinguishes them, declared by
the client at registration the same way its name is. Nullable: every existing agent genuinely
has none, and there is nothing to derive one from. Pure schema change.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``Agent.description``."""

    dependencies = [("facade", "0009_action_embedding")]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="description",
            field=models.TextField(blank=True, help_text="A description for the Agent", null=True),
        ),
    ]
