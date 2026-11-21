from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Per-user pins on bloks and materialized bloks."""

    dependencies = [
        ("facade", "0025_trigram_extension"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="blok",
            name="pinned_by",
            field=models.ManyToManyField(blank=True, help_text="The users that pinned this Blok. Rows go with the Blok in the database (ON DELETE CASCADE), as takt deletes bloks in SQL.", related_name="pinned_bloks", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="materializedblok",
            name="pinned_by",
            field=models.ManyToManyField(blank=True, help_text="The users that pinned this materialization. Rows go with it in the database (ON DELETE CASCADE), as takt deletes materializations in SQL.", related_name="pinned_materialized_bloks", to=settings.AUTH_USER_MODEL),
        ),
    ]
