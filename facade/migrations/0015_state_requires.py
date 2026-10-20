from django.db import migrations, models

# A task can say what its agent's state must hold for it to run, and an agent says on
# registration whether it tests that. Both columns are nullable: no task before this had
# requirements, and no agent said anything. The previous release never names the columns,
# so it runs on this schema.


class Migration(migrations.Migration):

    dependencies = [
        ("facade", "0014_catalogue_embeddings"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="features",
            field=models.JSONField(
                blank=True,
                help_text="What the agent said on its last registration it implements beyond the base protocol (e.g. 'state_requires'). A task with state requirements is only assigned to an agent that says it tests them.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="state_requires",
            field=models.JSONField(
                blank=True,
                help_text="What the agent's state must hold for the task to run ([{state, path, operator, value}]), sent with every dispatch of it. The agent tests it with the task's locks held; on a mismatch the task ends FAILED with code STATE_PRECONDITION.",
                null=True,
            ),
        ),
    ]
