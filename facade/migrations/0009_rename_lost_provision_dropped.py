# Generated by Django 3.2.14 on 2022-07-26 15:05

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0008_provision_lost'),
    ]

    operations = [
        migrations.RenameField(
            model_name='provision',
            old_name='lost',
            new_name='dropped',
        ),
    ]