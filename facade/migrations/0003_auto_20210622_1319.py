# Generated by Django 3.2.4 on 2021-06-22 13:19

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('herre', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('facade', '0002_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='repository',
            name='app',
        ),
        migrations.RemoveField(
            model_name='repository',
            name='mirror',
        ),
        migrations.RemoveField(
            model_name='repository',
            name='user',
        ),
        migrations.CreateModel(
            name='MirrorRepository',
            fields=[
                ('repository_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, serialize=False, to='facade.repository')),
                ('mirror', models.URLField(blank=True, default='None', null=True, unique=True)),
            ],
            bases=('facade.repository',),
        ),
        migrations.CreateModel(
            name='AppRepository',
            fields=[
                ('repository_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, serialize=False, to='facade.repository')),
                ('app', models.ForeignKey(help_text='The Associated App', null=True, on_delete=django.db.models.deletion.CASCADE, to='herre.herreapp')),
                ('user', models.ForeignKey(help_text='The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users', null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            bases=('facade.repository',),
        ),
    ]
