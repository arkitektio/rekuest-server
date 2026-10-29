from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Declared asks become declared interactions: an implementation's asks and informs, one
    table, told apart by `role`. The scopes they and the call paths need are renamed to one
    pattern (`rekuest_dynamic_ask`, `rekuest_dynamic_call`, `rekuest_dynamic_inform`)."""

    dependencies = [
        ("facade", "0005_declared_asks"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="implementationask",
            name="implementation_ask_unique_key",
        ),
        migrations.RenameModel(
            old_name="ImplementationAsk",
            new_name="ImplementationInteraction",
        ),
        migrations.AddField(
            model_name="implementationinteraction",
            name="role",
            field=models.CharField(
                choices=[("ASK", "Ask"), ("INFORM", "Inform")],
                db_default="ASK",
                default="ASK",
                help_text="Whether a task holds on it until it is answered (ASK) or shows it and goes on (INFORM).",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="implementationinteraction",
            name="implementation",
            field=models.ForeignKey(
                help_text="The implementation that declares it.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="interactions",
                to="facade.implementation",
            ),
        ),
        migrations.AlterField(
            model_name="implementationinteraction",
            name="blok",
            field=models.ForeignKey(
                help_text="The blok it is shown as.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="interacted_by",
                to="facade.blok",
            ),
        ),
        migrations.AlterField(
            model_name="implementationinteraction",
            name="key",
            field=models.CharField(
                help_text="What a task names it by: `<parameter>.<method>` of the action's interactions protocol.",
                max_length=1000,
            ),
        ),
        migrations.AlterField(
            model_name="implementationinteraction",
            name="description",
            field=models.TextField(blank=True, help_text="What it asks or shows, in a sentence.", null=True),
        ),
        migrations.AddConstraint(
            model_name="implementationinteraction",
            constraint=models.UniqueConstraint(
                fields=("implementation", "key"), name="implementation_interaction_unique_key"
            ),
        ),
        migrations.AlterField(
            model_name="agent",
            name="granted_scopes",
            field=models.JSONField(
                db_default=[],
                default=list,
                help_text="The scopes of the token the agent last registered with: which of its calls and bloks takt lets through (`rekuest_dependency_call`, `rekuest_dynamic_call`, `rekuest_dynamic_ask`, `rekuest_dynamic_inform`). Rewritten at every registration.",
            ),
        ),
        migrations.AlterField(
            model_name="blok",
            name="held_by_action",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Whether this provided Blok is an action's own (`ActionBlok`) or an implementation's declared ask or inform (`ImplementationInteraction`): declared inline, it is never listed or edited, and only an interaction's is materialized, for the task that shows it. takt deletes it once nothing holds or shows it.",
            ),
        ),
        migrations.AlterField(
            model_name="implementation",
            name="dynamic_asks",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Whether a task may ask through a blok made at ask time, or any blok of the organization, besides its declared asks. Also needs the agent's `rekuest_dynamic_ask` scope.",
            ),
        ),
        migrations.AlterField(
            model_name="implementation",
            name="dynamic_calls",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Whether a task may call actions it found at run time, not only its declared dependencies. Also needs the agent's `rekuest_dynamic_call` scope.",
            ),
        ),
        migrations.AlterField(
            model_name="implementation",
            name="dynamic_informs",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Whether a task may show a blok made at inform time, or any blok of the organization. Also needs the agent's `rekuest_dynamic_inform` scope.",
            ),
        ),
    ]
