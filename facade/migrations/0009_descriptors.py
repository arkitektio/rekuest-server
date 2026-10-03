import django.db.models.deletion
from django.db import migrations, models


def rows_from_json(apps, schema_editor):
    """Each hosted structure's descriptor list becomes Descriptor rows, in the declared order."""
    StructureDeclaration = apps.get_model("facade", "StructureDeclaration")
    Descriptor = apps.get_model("facade", "Descriptor")
    for structure in StructureDeclaration.objects.all():
        seen = set()
        for position, declared in enumerate(structure.declared_descriptors or []):
            key = declared.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            Descriptor.objects.create(structure=structure, key=key, type=declared.get("type") or "ANY", description=declared.get("description"), position=position)


class Migration(migrations.Migration):
    """A hosted structure's descriptors become rows of their own, so they can be listed and searched."""

    dependencies = [
        ('facade', '0008_automation'),
    ]

    operations = [
        # Out of the way of the new relation's name, and kept until its rows are written.
        migrations.RenameField(model_name="structuredeclaration", old_name="descriptors", new_name="declared_descriptors"),
        migrations.CreateModel(
            name='Descriptor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(help_text='The descriptor key, e.g. @mikro/n_channels', max_length=1000)),
                ('type', models.CharField(db_default='ANY', default='ANY', help_text='What its value is: INT, FLOAT, STRING, BOOL, LIST, or ANY when the service does not say', max_length=20)),
                ('description', models.TextField(blank=True, help_text='What the service says the descriptor means', null=True)),
                ('position', models.PositiveIntegerField(db_default=0, default=0, help_text="Its place in the service's declaration")),
                ('structure', models.ForeignKey(help_text='The hosted structure whose objects carry it', on_delete=django.db.models.deletion.CASCADE, related_name='descriptors', to='facade.structuredeclaration')),
            ],
            options={
                'ordering': ['structure_id', 'position', 'id'],
                'indexes': [models.Index(fields=['key'], name='descriptor_key_idx')],
                'constraints': [models.UniqueConstraint(fields=('structure', 'key'), name='descriptor_unique_key_per_structure')],
            },
        ),
        # The next provisioning pass would rewrite them anyway; carried over so nothing is missing meanwhile.
        migrations.RunPython(rows_from_json, migrations.RunPython.noop),
        migrations.RemoveField(model_name="structuredeclaration", name="declared_descriptors"),
    ]
