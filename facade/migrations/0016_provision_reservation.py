# Generated by Django 3.1.7 on 2021-03-26 12:38

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0015_remove_provision_node'),
    ]

    operations = [
        migrations.AddField(
            model_name='provision',
            name='reservation',
            field=models.OneToOneField(blank=True, help_text='The Reservation that created this Provision', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='provisions', to='facade.reservation'),
        ),
    ]
