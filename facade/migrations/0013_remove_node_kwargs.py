# Generated by Django 3.2.14 on 2022-08-18 08:33

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0012_reservation_allow_auto_request'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='node',
            name='kwargs',
        ),
    ]
