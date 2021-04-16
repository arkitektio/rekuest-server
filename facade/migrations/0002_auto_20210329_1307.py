# Generated by Django 3.1.7 on 2021-03-29 13:07

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('facade', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='template',
            name='creator',
            field=models.ForeignKey(help_text='Who created this template on this instance', null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='template',
            name='node',
            field=models.ForeignKey(help_text='The node this template is implementatig', on_delete=django.db.models.deletion.CASCADE, to='facade.node'),
        ),
        migrations.AddField(
            model_name='template',
            name='provider',
            field=models.ForeignKey(help_text='The associated provider for this Template', on_delete=django.db.models.deletion.CASCADE, to='facade.baseprovider'),
        ),
        migrations.AddConstraint(
            model_name='service',
            constraint=models.UniqueConstraint(fields=('inward', 'port'), name='Unique Service'),
        ),
        migrations.AddField(
            model_name='reservation',
            name='creator',
            field=models.ForeignKey(help_text='This provision creator', max_length=1000, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='reservation',
            name='node',
            field=models.ForeignKey(blank=True, help_text='The node this reservation connects', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='facade.node'),
        ),
        migrations.AddField(
            model_name='reservation',
            name='parent',
            field=models.ForeignKey(blank=True, help_text='The Provisions parent', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='facade.reservation'),
        ),
        migrations.AddField(
            model_name='reservation',
            name='pod',
            field=models.ForeignKey(blank=True, help_text='The pod this reservation connects', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='facade.pod'),
        ),
        migrations.AddField(
            model_name='reservation',
            name='template',
            field=models.ForeignKey(blank=True, help_text='The template this reservation connects', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='reservations', to='facade.template'),
        ),
        migrations.AddField(
            model_name='provision',
            name='creator',
            field=models.ForeignKey(help_text='This provision creator', max_length=1000, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='provision',
            name='parent',
            field=models.ForeignKey(blank=True, help_text='The Provisions parent', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='facade.provision'),
        ),
        migrations.AddField(
            model_name='provision',
            name='reservation',
            field=models.ForeignKey(blank=True, help_text='The Reservation that created this Provision', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='provisions', to='facade.reservation'),
        ),
        migrations.AddField(
            model_name='provision',
            name='template',
            field=models.ForeignKey(blank=True, help_text='The node this provision connects', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='provisions', to='facade.template'),
        ),
        migrations.AddField(
            model_name='pod',
            name='provision',
            field=models.ForeignKey(help_text='The provision that created this pod', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='created_pods', to='facade.provision'),
        ),
        migrations.AddField(
            model_name='pod',
            name='template',
            field=models.ForeignKey(help_text='The template that created this pod', on_delete=django.db.models.deletion.CASCADE, related_name='pods', to='facade.template'),
        ),
        migrations.AddConstraint(
            model_name='datapoint',
            constraint=models.UniqueConstraint(fields=('inward', 'port'), name='unique datapoint'),
        ),
        migrations.AddField(
            model_name='datamodel',
            name='point',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='models', to='facade.datapoint'),
        ),
        migrations.AddField(
            model_name='commission',
            name='pod',
            field=models.ForeignKey(help_text='Which pod are we commisssioning?', on_delete=django.db.models.deletion.CASCADE, related_name='commisions', to='facade.pod'),
        ),
        migrations.AddField(
            model_name='assignation',
            name='creator',
            field=models.ForeignKey(help_text='The creator is this assignation', max_length=1000, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='assignation',
            name='node',
            field=models.ForeignKey(blank=True, help_text='The Node this assignation is having', null=True, on_delete=django.db.models.deletion.CASCADE, to='facade.node'),
        ),
        migrations.AddField(
            model_name='assignation',
            name='parent',
            field=models.ForeignKey(blank=True, help_text='The Assignations parent', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='facade.assignation'),
        ),
        migrations.AddField(
            model_name='assignation',
            name='pod',
            field=models.ForeignKey(blank=True, help_text='The pod this assignation connects to', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='assignations', to='facade.pod'),
        ),
        migrations.AddField(
            model_name='assignation',
            name='reservation',
            field=models.ForeignKey(blank=True, help_text='Which reservation are we assigning to', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='assignations', to='facade.reservation'),
        ),
        migrations.AddField(
            model_name='assignation',
            name='template',
            field=models.ForeignKey(blank=True, help_text='The Template this assignation is using', null=True, on_delete=django.db.models.deletion.CASCADE, to='facade.template'),
        ),
        migrations.AddConstraint(
            model_name='template',
            constraint=models.UniqueConstraint(fields=('node', 'params'), name='A template has unique params for every node'),
        ),
        migrations.AddField(
            model_name='serviceprovider',
            name='service',
            field=models.OneToOneField(help_text='The Associated Service for this Provider', on_delete=django.db.models.deletion.CASCADE, to='facade.service'),
        ),
        migrations.AddConstraint(
            model_name='pod',
            constraint=models.UniqueConstraint(fields=('template', 'name'), name='A pod needs to uniquely identify with a name for a template'),
        ),
        migrations.AddField(
            model_name='node',
            name='repository',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='nodes', to='facade.apprepository'),
        ),
        migrations.AddConstraint(
            model_name='datamodel',
            constraint=models.UniqueConstraint(fields=('point', 'identifier'), name='unique identifier for point'),
        ),
        migrations.AddField(
            model_name='apprepository',
            name='user',
            field=models.ForeignKey(help_text='The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users', null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='appprovider',
            name='user',
            field=models.ForeignKey(help_text='The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users', null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name='node',
            constraint=models.UniqueConstraint(fields=('repository', 'package', 'interface'), name='package, interface, repository cannot be the same'),
        ),
        migrations.AddConstraint(
            model_name='apprepository',
            constraint=models.UniqueConstraint(fields=('client_id', 'user'), name='No multiple Repositories for same App and User allowed'),
        ),
        migrations.AddConstraint(
            model_name='appprovider',
            constraint=models.UniqueConstraint(fields=('client_id', 'user'), name='No multiple Providers for same App and User allowed'),
        ),
    ]