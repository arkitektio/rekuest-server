# Generated by Django 3.2.12 on 2022-07-19 11:41

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0003_auto_20220709_1633'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='template',
            options={'permissions': [('providable', 'Can provide this template')]},
        ),
    ]
