# Generated by Django 3.1.7 on 2021-03-04 11:15

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Service',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.CharField(help_text='The version of the bergen API this endpoint uses', max_length=100)),
                ('inward', models.CharField(help_text='Inward facing hostname (for Docker powered access)', max_length=100)),
                ('outward', models.CharField(help_text='Outward facing hostname for external clients', max_length=100)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('port', models.IntegerField(help_text='Listening port')),
            ],
        ),
        migrations.RemoveConstraint(
            model_name='serviceprovider',
            name='No multiple Providers for same Service allowed',
        ),
        migrations.RemoveField(
            model_name='serviceprovider',
            name='host',
        ),
        migrations.RemoveField(
            model_name='serviceprovider',
            name='port',
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(fields=('inward', 'port'), name='Unique Service'),
        ),
        migrations.AddField(
            model_name='serviceprovider',
            name='service',
            field=models.ForeignKey(default=1, help_text='The Associated Service for this Provider', on_delete=django.db.models.deletion.CASCADE, to='facade.service', unique=True),
            preserve_default=False,
        ),
    ]