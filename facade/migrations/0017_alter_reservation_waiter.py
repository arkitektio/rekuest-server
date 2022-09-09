# Generated by Django 3.2.14 on 2022-08-21 14:06

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0016_auto_20220821_1404'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reservation',
            name='waiter',
            field=models.ForeignKey(help_text='This Reservations app', max_length=1000, on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='facade.waiter'),
        ),
    ]
