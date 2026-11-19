from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0023_shortcut_names_its_agent'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='traceparent',
            field=models.CharField(blank=True, help_text='Where in a trace the task was assigned (W3C Trace Context), sent with every dispatch of it: what its agent does for it belongs to that trace. Written by takt; null where nobody traces.', max_length=64, null=True),
        ),
    ]
