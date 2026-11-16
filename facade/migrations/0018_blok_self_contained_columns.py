"""The columns blok language v2 moved into the tree go: ``locals``, ``queries``, ``subscriptions`` (0017 emptied the tables)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0017_blok_self_contained"),
    ]

    operations = [
        migrations.RemoveField(model_name="blok", name="locals"),
        migrations.RemoveField(model_name="blok", name="queries"),
        migrations.RemoveField(model_name="blok", name="subscriptions"),
        migrations.RemoveField(model_name="blokrevision", name="locals"),
        migrations.RemoveField(model_name="blokrevision", name="queries"),
        migrations.RemoveField(model_name="blokrevision", name="subscriptions"),
        migrations.AlterField(
            model_name="blok",
            name="context",
            field=models.CharField(db_default="PANEL", default="PANEL", help_text="What draws it, as its `<Blok context=...>` says (rekuest_core ComponentContext): PANEL, SCENE or LOGO.", max_length=16),
        ),
        migrations.AlterField(
            model_name="blokrevision",
            name="context",
            field=models.CharField(db_default="PANEL", default="PANEL", help_text="What draws this revision, as its `<Blok context=...>` says (rekuest_core ComponentContext).", max_length=16),
        ),
        migrations.AlterField(
            model_name="actionblok",
            name="context",
            field=models.CharField(help_text="Where a client shows a run with it (rekuest_core ComponentContext): PANEL or SCENE. One blok per context and phase.", max_length=16),
        ),
        migrations.AlterField(
            model_name="blok",
            name="components",
            field=models.JSONField(db_default=[], default=list, help_text="The component tree of this Blok (blok language v2): one `<Blok kind context>` root, as rekuest_core ComponentNodeInput, its declarations (`<local>`, `<query>`, `<subscription>`) included."),
        ),
        migrations.AlterField(
            model_name="blok",
            name="resolved_locals",
            field=models.JSONField(db_default=[], default=list, help_text="The `<local>` declarations of its tree as they resolve (rekuest_core.vocabulary.infer), each its node id plus its port with a kind: derived, recomputed on every write, never hashed."),
        ),
        migrations.AlterField(
            model_name="blokrevision",
            name="resolved_locals",
            field=models.JSONField(db_default=[], default=list, help_text="The `<local>` declarations of this revision's tree as they resolve, each its node id plus its port with a kind: derived, never hashed."),
        ),
        migrations.AlterField(
            model_name="implementation",
            name="logo",
            field=models.ForeignKey(blank=True, help_text='The agent\'s action logo blok (`<Blok kind="action" context="logo">`) that draws this implementation\'s action, if it declared one.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="drawn_implementations", to="facade.blok"),
        ),
    ]
