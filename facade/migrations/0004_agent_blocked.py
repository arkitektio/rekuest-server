# Generated by Django 3.2.19 on 2023-05-28 10:36

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0003_agent_on_instance'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='blocked',
            field=models.BooleanField(default=False, help_text='If this Agent is blocked, it will not be used for provision, nor will it be able to provide'),
        ),
    ]