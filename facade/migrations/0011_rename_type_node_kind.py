# Generated by Django 3.2.14 on 2022-08-17 09:34

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0010_reservation_hash'),
    ]

    operations = [
        migrations.RenameField(
            model_name='node',
            old_name='type',
            new_name='kind',
        ),
    ]