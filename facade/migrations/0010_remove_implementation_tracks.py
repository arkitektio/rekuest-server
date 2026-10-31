from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0009_object_descriptors'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='implementation',
            name='tracks',
        ),
    ]
