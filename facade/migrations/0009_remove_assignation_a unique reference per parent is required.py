# Generated by Django 3.2.19 on 2023-05-31 09:55

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0008_reservation_equal reservation on this app by this waiter is already in place'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='assignation',
            name='A unique reference per parent is required',
        ),
    ]
