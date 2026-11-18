"""An agent's commands are kept in the database, and a session's position is moved with what it counts.

``Command`` and ``CommandCursor`` replace the redis streams a socket agent's commands waited
in: a command is written in the transaction that makes it true, so there is nothing to make
up for when redis and the database disagree. Commands still waiting in redis when this is
applied are not carried over; an agent's own cursor (``commands_seen``) carries the numbering.

``Session.claimed_pos`` / ``claimed_at`` go: a frame's position is moved in the transaction
that projects it, so there is no claim to keep between the two.
"""

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0020_patch_lifecycle_qualifiers"),
    ]

    operations = [
        migrations.CreateModel(
            name="Command",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pos", models.PositiveBigIntegerField(blank=True, help_text="The command's position in the agent's stream. NULL until the connection holding the agent's lease numbers it.", null=True)),
                ("kind", models.CharField(help_text="The frame's type (ASSIGN, CANCEL, ASSIGN_RESPONSE, ...), or EVENT for the mirror of a task event.", max_length=64)),
                ("task", models.BigIntegerField(blank=True, help_text="The id of the task the command is about, if it is about one. Not a foreign key: takt deletes tasks with SQL of its own.", null=True)),
                ("event", models.BigIntegerField(blank=True, help_text="For an EVENT: the id of the TaskEvent it mirrors. The frame is built from that row when it is sent.", null=True)),
                ("frame", models.TextField(blank=True, help_text="The serialized frame, without its position. NULL for an EVENT.", null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_default=django.db.models.functions.datetime.Now())),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="commands", to="facade.agent")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["agent", "pos"], name="command_agent_pos"),
                    models.Index(fields=["task", "kind"], name="command_task_kind"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CommandCursor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("numbered", models.PositiveBigIntegerField(db_default=0, default=0, help_text="The highest position a command of this agent was given.")),
                ("agent", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="command_cursor", to="facade.agent")),
            ],
        ),
        migrations.RemoveField(
            model_name="session",
            name="claimed_at",
        ),
        migrations.RemoveField(
            model_name="session",
            name="claimed_pos",
        ),
    ]
