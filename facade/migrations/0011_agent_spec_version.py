from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0010_remove_implementation_tracks'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='spec_version',
            field=models.CharField(blank=True, help_text='The arkitekt-spec the agent said on its last registration it runs (e.g. 8.0.0). Null for an agent that sends no REGISTER, such as a hook agent.', max_length=100, null=True),
        ),
    ]
