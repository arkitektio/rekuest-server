"""Signals and triggers: services announce what happened, users' triggers decide what runs.

Adds ``Signal`` (one announced event, unique per service + id), ``Trigger`` (a user's rule over
signals) and, on ``Task``, the ``signal`` / ``trigger`` that caused it plus ``trigger_depth``
(the loop guard). Every new FK is SET_NULL, so deleting a signal or trigger keeps the runs.
Pure schema change.
"""


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('authentikate', '0006_alter_app_identifier_alter_release_unique_together'),
        ('facade', '0012_schedules'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='trigger_depth',
            field=models.PositiveSmallIntegerField(db_default=0, default=0, help_text="How many trigger firings lead to this task (its causing task's depth + 1); bounds trigger loops"),
        ),
        migrations.CreateModel(
            name='Signal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('service', models.CharField(help_text='The service that sent it (its `rekuest.service_agents` name)', max_length=200)),
                ('signal_id', models.CharField(help_text="The service's id for the signal; a resend with the same id is a no-op", max_length=200)),
                ('kind', models.CharField(choices=[('CREATED', 'Created'), ('UPDATED', 'Updated'), ('DELETED', 'Deleted')], help_text='What happened to the object', max_length=20)),
                ('identifier', models.CharField(help_text="The object's structure identifier, e.g. @mikro/arraydataset", max_length=1000)),
                ('object', models.CharField(help_text="The object's id within its structure", max_length=1000)),
                ('descriptors', models.JSONField(blank=True, default=dict, help_text="The object's descriptors (flat key → value), matched against triggers' conditions and ports' `requires`")),
                ('causing_root', models.CharField(blank=True, help_text="The verified root task id of the causing tree (the token's `rtk`)", max_length=200, null=True)),
                ('occurred_at', models.DateTimeField(blank=True, help_text='When it happened, per the service', null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('processed_at', models.DateTimeField(blank=True, help_text='When `fire_triggers` matched it; null = not yet', null=True)),
                ('causing_task', models.ForeignKey(blank=True, help_text="The task the object was created in — from a provenance token rekuest verified, never from the service's word", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='caused_signals', to='facade.task')),
                ('organization', models.ForeignKey(help_text='The organization the object belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='signals', to='authentikate.organization')),
            ],
        ),
        migrations.AddField(
            model_name='task',
            name='signal',
            field=models.ForeignKey(blank=True, help_text='The signal that caused this task, if a trigger fired it', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='facade.signal'),
        ),
        migrations.CreateModel(
            name='Trigger',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='A human-readable name for the trigger', max_length=200)),
                ('enabled', models.BooleanField(default=True, help_text='A disabled trigger fires nothing')),
                ('kind', models.CharField(choices=[('CREATED', 'Created'), ('UPDATED', 'Updated'), ('DELETED', 'Deleted')], help_text='The signal kind it reacts to', max_length=20)),
                ('identifier', models.CharField(help_text='The structure identifier it reacts to, e.g. @mikro/arraydataset', max_length=1000)),
                ('conditions', models.JSONField(blank=True, default=list, help_text='Extra descriptor conditions (requires-style: key, operator, value)')),
                ('compiled_jsonpath', models.TextField(blank=True, help_text='`conditions` compiled to a PostgreSQL JSONPath predicate; null = no extra conditions', null=True)),
                ('interface', models.CharField(blank=True, help_text='The implementation interface on the pinned agent', max_length=1000, null=True)),
                ('port', models.CharField(help_text='The STRUCTURE arg that receives the signalled object', max_length=1000)),
                ('args', models.JSONField(blank=True, default=dict, help_text='The other args of every run')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('consecutive_failures', models.PositiveIntegerField(default=0, help_text='Firings in a row that could not create a run')),
                ('last_error', models.TextField(blank=True, help_text='Why the last firing did not create a run', null=True)),
                ('action', models.ForeignKey(help_text='The action every run assigns', on_delete=django.db.models.deletion.CASCADE, related_name='triggers', to='facade.action')),
                ('agent', models.ForeignKey(blank=True, help_text='Pin runs to this agent (with `interface`)', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='triggers', to='facade.agent')),
                ('caller', models.ForeignKey(help_text='The owner: runs are assigned as this identity, its organization scopes the trigger and the signals it sees', on_delete=django.db.models.deletion.CASCADE, related_name='triggers', to='facade.caller')),
            ],
        ),
        migrations.AddField(
            model_name='task',
            name='trigger',
            field=models.ForeignKey(blank=True, help_text='The trigger that fired this task, if any', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='facade.trigger'),
        ),
        migrations.AddIndex(
            model_name='signal',
            index=models.Index(condition=models.Q(('processed_at__isnull', True)), fields=['received_at'], name='signal_unprocessed_idx'),
        ),
        migrations.AddIndex(
            model_name='signal',
            index=models.Index(fields=['organization', '-received_at'], name='signal_org_received_idx'),
        ),
        migrations.AddConstraint(
            model_name='signal',
            constraint=models.UniqueConstraint(fields=('service', 'signal_id'), name='signal_unique_per_service'),
        ),
        migrations.AddIndex(
            model_name='trigger',
            index=models.Index(condition=models.Q(('enabled', True)), fields=['kind', 'identifier'], name='trigger_enabled_match_idx'),
        ),
        migrations.AddIndex(
            model_name='trigger',
            index=models.Index(fields=['caller', '-created_at'], name='trigger_caller_created_idx'),
        ),
    ]
