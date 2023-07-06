# Generated by Django 3.2.19 on 2023-06-08 14:49

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0013_auto_20230608_1438'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcase',
            name='name',
            field=models.CharField(blank=True, max_length=2000, null=True),
        ),
        migrations.AlterField(
            model_name='testcase',
            name='node',
            field=models.ForeignKey(help_text='The node this test belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='testcases', to='facade.node'),
        ),
    ]