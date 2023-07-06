# Generated by Django 3.2.19 on 2023-05-28 11:43

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0004_agent_blocked'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agent',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'Active'), ('KICKED', 'Just kicked'), ('DISCONNECTED', 'Disconnected'), ('VANILLA', 'Complete Vanilla Scenario after a forced restart of')], default='VANILLA', help_text='The Status of this Agent', max_length=1000),
        ),
    ]
