# Generated by Django 3.1.7 on 2021-02-25 11:19

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0013_auto_20210225_1113'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='provider',
            name='Only one provider per app',
        ),
        migrations.AddField(
            model_name='provider',
            name='internal',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='provider',
            name='app',
            field=models.CharField(default=uuid.uuid4, help_text='Do we have an external client? The Client ID of the App connecting, Default to internal if this is internal', max_length=600, unique=True),
        ),
        migrations.AlterField(
            model_name='provider',
            name='name',
            field=models.CharField(default='Nana', help_text='This providers Name', max_length=2000),
        ),
    ]
