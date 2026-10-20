import pgvector.django.vector
from django.db import migrations

# What services host and emit is searched by meaning, like actions: hosted structures, their
# descriptors and the signal declarations each get a nullable vector column. No backfill: the
# next provisioning pass (``catalogue`` at migrate, then every upkeep) saves each row through
# ``update_or_create``, and the mixin embeds a row whose vector is still NULL. The previous
# release never names the columns, so it runs on this schema.

HELP = "Unit-length embedding of name + description, by the release's embedding model; NULL when there is no text to embed"


class Migration(migrations.Migration):

    dependencies = [
        ("facade", "0013_rule_policy"),
    ]

    operations = [
        migrations.AddField(model_name="structuredeclaration", name="embedding", field=pgvector.django.vector.VectorField(blank=True, dimensions=256, editable=False, help_text=HELP, null=True)),
        migrations.AddField(model_name="descriptor", name="embedding", field=pgvector.django.vector.VectorField(blank=True, dimensions=256, editable=False, help_text=HELP, null=True)),
        migrations.AddField(model_name="signaldeclaration", name="embedding", field=pgvector.django.vector.VectorField(blank=True, dimensions=256, editable=False, help_text=HELP, null=True)),
    ]
