import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Services become rows of their own: what they host and emit hangs on them, not on an agent."""

    dependencies = [
        ("facade", "0006_agent_name_help_text"),
    ]

    operations = [
        migrations.CreateModel(
            name="Service",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="The name the service is configured under (rekuest.services[].name)", max_length=1000, unique=True)),
                ("identifier", models.CharField(blank=True, help_text="The identity the service signs as, e.g. live.arkitekt.mikro", max_length=1000, null=True)),
                ("description", models.TextField(blank=True, help_text="What the service says it is", null=True)),
            ],
        ),
        migrations.CreateModel(
            name="StructureDeclaration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("identifier", models.CharField(help_text="The structure identifier, e.g. @mikro/arraydataset", max_length=1000, unique=True)),
                ("label", models.CharField(blank=True, help_text="What the service calls one such object", max_length=1000, null=True)),
                ("description", models.TextField(blank=True, help_text="What the service says about the structure", null=True)),
                ("descriptors", models.JSONField(blank=True, db_default=[], default=list, help_text="The descriptors its objects carry: [{key, type, description}]")),
                ("service", models.ForeignKey(help_text="The service that hosts it", on_delete=django.db.models.deletion.CASCADE, related_name="structures", to="facade.service")),
            ],
        ),
        # Signal declarations move from the service's agent to the service. The rows are a mirror
        # of the manifests and are rewritten by the next provisioning pass (at start, then every
        # five minutes), so they are dropped here rather than carried over.
        migrations.RunSQL("DELETE FROM facade_signaldeclaration", migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name="signaldeclaration", name="signal_declaration_unique"),
        migrations.RemoveField(model_name="signaldeclaration", name="agent"),
        migrations.AddField(
            model_name="signaldeclaration",
            name="service",
            field=models.ForeignKey(help_text="The service that emits it", on_delete=django.db.models.deletion.CASCADE, related_name="signals", to="facade.service"),
        ),
        migrations.AddConstraint(
            model_name="signaldeclaration",
            constraint=models.UniqueConstraint(fields=("service", "identifier", "kind"), name="signal_declaration_unique"),
        ),
        migrations.AlterField(
            model_name="signal",
            name="service",
            field=models.CharField(help_text="The service that sent it (its `rekuest.services` name)", max_length=200),
        ),
    ]
