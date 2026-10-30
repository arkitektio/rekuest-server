from django.db import migrations, models


class Migration(migrations.Migration):
    """An implementation's runtime formula and an agent's hardware, for the frontend's estimate.
    Both nullable, so the previous release still runs on this schema."""

    dependencies = [
        ("facade", "0006_declared_interactions"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="hardware",
            field=models.JSONField(
                blank=True,
                help_text="What this agent's machine has (cpu_count, memory_mib, gpus), as it stated on its last registration. Self-declared, for showing an estimate.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="implementation",
            name="estimated_runtime",
            field=models.JSONField(
                blank=True,
                help_text="A formula for how long this implementation runs, in seconds: a pure blok call and the text it was written as. The frontend evaluates it; the server only checks and stores it.",
                null=True,
            ),
        ),
    ]
