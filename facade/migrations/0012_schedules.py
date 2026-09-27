"""Schedules: recurring assignments, materialized one delayed task at a time.

Adds ``Schedule`` and ``Task.schedule`` (SET_NULL: deleting a schedule keeps its run history),
plus the partial index behind "does this schedule have an open run?". Pure schema change.
"""


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0011_task_not_before'),
    ]

    operations = [
        migrations.CreateModel(
            name='Schedule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='A human-readable name for the schedule', max_length=200)),
                ('interface', models.CharField(blank=True, help_text='The implementation interface on the pinned agent', max_length=1000, null=True)),
                ('args', models.JSONField(blank=True, default=dict, help_text='The args every run is assigned with')),
                ('interval_seconds', models.PositiveIntegerField(blank=True, help_text="Run every N seconds, aligned to the schedule's creation. Exclusive with ``cron``.", null=True)),
                ('cron', models.CharField(blank=True, help_text='A five-field cron line, read in ``timezone``. Exclusive with ``interval_seconds``.', max_length=200, null=True)),
                ('timezone', models.CharField(default='UTC', help_text='The IANA zone a cron line is read in (DST included)', max_length=64)),
                ('ephemeral_runs', models.BooleanField(default=False, help_text='Create the runs as ephemeral tasks (housekeeping sweeps: retention may drop them early)')),
                ('enabled', models.BooleanField(default=True, help_text='A disabled schedule creates no runs')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('consecutive_failures', models.PositiveIntegerField(default=0, help_text='Runs in a row that ended FAILED or CRITICAL; reset by a successful one')),
                ('last_error', models.TextField(blank=True, help_text='Why the last run failed, or why the next one could not be created', null=True)),
                ('refill_after', models.DateTimeField(blank=True, help_text='Creating the next run failed; not retried before then', null=True)),
                ('action', models.ForeignKey(help_text='The action every run assigns', on_delete=django.db.models.deletion.CASCADE, related_name='schedules', to='facade.action')),
                ('agent', models.ForeignKey(blank=True, help_text='Pin every run to this agent (with ``interface``); null = resolve an agent for the action per run', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='schedules', to='facade.agent')),
                ('caller', models.ForeignKey(help_text="The identity every run is assigned as — its organization scopes the schedule, and it namespaces the runs' references", on_delete=django.db.models.deletion.CASCADE, related_name='schedules', to='facade.caller')),
            ],
        ),
        migrations.AddField(
            model_name='task',
            name='schedule',
            field=models.ForeignKey(blank=True, help_text='The schedule this task is a run of, if any', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='facade.schedule'),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(condition=models.Q(('is_done', False), ('schedule__isnull', False)), fields=['schedule'], name='task_schedule_open_idx'),
        ),
        migrations.AddIndex(
            model_name='schedule',
            index=models.Index(fields=['caller', '-created_at'], name='schedule_caller_created_idx'),
        ),
        migrations.AddConstraint(
            model_name='schedule',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('cron__isnull', True), ('interval_seconds__isnull', False)), models.Q(('cron__isnull', False), ('interval_seconds__isnull', True)), _connector='OR'), name='schedule_interval_xor_cron'),
        ),
    ]
