# Generated by Django 3.1.7 on 2021-03-29 13:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('herre', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='herreuser',
            name='roles',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
