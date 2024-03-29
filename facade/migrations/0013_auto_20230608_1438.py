# Generated by Django 3.2.19 on 2023-06-08 14:38

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0012_auto_20230608_1434'),
    ]

    operations = [
        migrations.AddField(
            model_name='testresult',
            name='template',
            field=models.ForeignKey(default=1, on_delete=django.db.models.deletion.CASCADE, related_name='results', to='facade.template'),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='testcase',
            name='node',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='testcases', to='facade.node'),
        ),
        migrations.AlterField(
            model_name='testresult',
            name='case',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='results', to='facade.testcase'),
        ),
    ]
