from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """What an implementation can handle per argument, and its runtime formula compiled for the
    action query. Everything new is nullable or a new table, so the previous release still runs."""

    dependencies = [
        ("facade", "0007_estimated_runtime_hardware"),
    ]

    operations = [
        migrations.AlterField(
            model_name="implementation",
            name="estimated_runtime",
            field=models.JSONField(
                blank=True,
                help_text="A formula for how long this implementation runs, in seconds: a pure blok call and the text it was written as, and optionally a richer one for the form.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="implementation",
            name="estimated_runtime_path",
            field=models.TextField(
                blank=True,
                help_text="`estimated_runtime` compiled to a PostgreSQL JSONPath over the object and $agent: what the action query filters and sorts by.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="implementation",
            name="estimated_runtime_port",
            field=models.CharField(
                blank=True,
                help_text="The key of the one structure argument `estimated_runtime` reads, if any: it applies to objects of that argument's identifier.",
                max_length=1000,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="ImplementationFit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("port_key", models.CharField(help_text="The key of the argument.", max_length=1000)),
                ("compiled_jsonpath", models.TextField(help_text="The formula compiled to a PostgreSQL JSONPath predicate over the object and $agent.")),
                ("call", models.JSONField(help_text="The pure blok call the frontend and the agent evaluate.")),
                ("source", models.TextField(blank=True, help_text="The formula as its author wrote it.", null=True)),
                (
                    "implementation",
                    models.ForeignKey(
                        help_text="The implementation it is about.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fits",
                        to="facade.implementation",
                    ),
                ),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("implementation", "port_key"), name="implementation_fit_unique_port")],
            },
        ),
    ]
