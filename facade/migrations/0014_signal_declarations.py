"""``SignalDeclaration``: the signals a service says it emits, read from its manifest.

Lets ``createTrigger`` check a trigger against what is actually emitted, and a UI list it.
Pure schema change.
"""


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0013_signals_triggers'),
    ]

    operations = [
        migrations.CreateModel(
            name='SignalDeclaration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('identifier', models.CharField(help_text='The structure identifier of the objects signalled', max_length=1000)),
                ('kind', models.CharField(choices=[('CREATED', 'Created'), ('UPDATED', 'Updated'), ('DELETED', 'Deleted')], help_text='What happens to them', max_length=20)),
                ('descriptor_keys', models.JSONField(blank=True, default=list, help_text='The descriptor keys each signal carries')),
                ('description', models.TextField(blank=True, help_text='What the service says about the signal', null=True)),
                ('agent', models.ForeignKey(help_text="The service's HookAgent", on_delete=django.db.models.deletion.CASCADE, related_name='signal_declarations', to='facade.agent')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('agent', 'identifier', 'kind'), name='signal_declaration_unique')],
            },
        ),
    ]
