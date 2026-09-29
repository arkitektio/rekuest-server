"""Session positions, task steps and effects, and uniqueness for state patches.

``Session.projected_pos`` (+ the ``claimed_pos``/``claimed_at`` claim) is the watermark that
makes a numbered frame a no-op when it is sent again; ``TaskEvent``/``Patch`` carry the frame's
``agent_pos``/``agent_ts``/``step``; ``TaskEvent.effect``/``value`` hold EFFECT events; ``Task.parent_step`` (unique per parent)
records the step a child call took; drawers
get ``agent_minted`` (see ``docs/design/journal.md``).

``Patch`` also gets unique (session, global_rev, state). Agents bump ``global_rev`` once per
patch, so an existing duplicate can only be a re-sent patch, and reconstruction applied it twice.
Dedupe and constraint run in this ONE migration, under a table lock, like 0008: a data migration
followed by a schema one would drop the lock in between and let a replica still on the previous
release insert a fresh duplicate. Survivor: the lowest id, the patch as first received. Nothing
references ``Patch``, so the losers can simply go. Patches without a session (NULL) are distinct
in Postgres and left alone.
"""


from django.db import migrations, models



def dedupe_patches(apps, schema_editor):
    """One row per (session, global_rev, state). Survivor: the lowest id."""
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("LOCK TABLE facade_patch IN SHARE ROW EXCLUSIVE MODE")
    schema_editor.execute(
        """
        DELETE FROM facade_patch WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY session_id, global_rev, state_id
                    ORDER BY id
                ) AS position
                FROM facade_patch WHERE session_id IS NOT NULL
            ) ranked WHERE position > 1
        )
        """
    )
    # Django's FKs are DEFERRABLE INITIALLY DEFERRED: settle the pending checks before the
    # ALTER TABLE that follows (see 0008).
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):

    dependencies = [
        ('facade', '0014_signal_declarations'),
    ]

    operations = [
        migrations.AddField(
            model_name='memorydrawer',
            name='agent_minted',
            field=models.BooleanField(default=False, help_text="The agent minted this drawer's reference (a numbered SHELVE): the agent addresses it by resource_id, and COLLECT names it by resource_id. False for drawers of older agents, which reference the pk."),
        ),
        migrations.AddField(
            model_name='patch',
            name='agent_pos',
            field=models.PositiveBigIntegerField(blank=True, help_text='The session position (pos) of the frame that carried this patch. NULL for agents without numbering.', null=True),
        ),
        migrations.AddField(
            model_name='patch',
            name='agent_ts',
            field=models.DateTimeField(blank=True, help_text="When the agent recorded the patch (the frame's agent_ts). NULL for agents without numbering.", null=True),
        ),
        migrations.AddField(
            model_name='patch',
            name='old_value',
            field=models.JSONField(blank=True, help_text='The value the patch replaced, when the agent reported it (debugging and tracing only; never used to reconstruct state)', null=True),
        ),
        migrations.AddField(
            model_name='patch',
            name='step',
            field=models.PositiveBigIntegerField(blank=True, help_text="The changing task's step (the frame's task_step). NULL for agents without numbering and patches outside a task.", null=True),
        ),
        migrations.AddField(
            model_name='session',
            name='claimed_at',
            field=models.DateTimeField(blank=True, help_text='When claimed_pos was claimed', null=True),
        ),
        migrations.AddField(
            model_name='session',
            name='claimed_pos',
            field=models.PositiveBigIntegerField(default=0, help_text='The position a backend is projecting right now (claimed_pos = projected_pos + 1 while one is in flight, else equal). Another backend waits for it, or takes it over once claimed_at is stale.'),
        ),
        migrations.AddField(
            model_name='session',
            name='projected_pos',
            field=models.PositiveBigIntegerField(default=0, help_text='Every numbered frame of this session up to this position is handled: a resend at or below it is skipped, and JOURNAL_ACK claims it (docs/design/journal.md).'),
        ),
        migrations.AddField(
            model_name='taskevent',
            name='agent_pos',
            field=models.PositiveBigIntegerField(blank=True, help_text='The session position (pos) of the report that wrote this event. NULL for server-written events and for agents without numbering.', null=True),
        ),
        migrations.AddField(
            model_name='taskevent',
            name='agent_ts',
            field=models.DateTimeField(blank=True, help_text="When the agent recorded the report (the frame's agent_ts). NULL for server-written events and for agents without numbering.", null=True),
        ),
        migrations.AddField(
            model_name='taskevent',
            name='effect',
            field=models.CharField(blank=True, help_text='EFFECT events: what the task took from outside itself (NOW, RANDOM, SLEEP).', max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='taskevent',
            name='step',
            field=models.PositiveBigIntegerField(blank=True, help_text="The report's step within its task (the frame's task_step). A task's history is its events, patches and child tasks (parent_step) in step order. NULL for server-written events.", null=True),
        ),
        migrations.AddField(
            model_name='taskevent',
            name='value',
            field=models.JSONField(blank=True, help_text='EFFECT events: the value taken (NOW: epoch seconds, RANDOM: hex, SLEEP: the deadline in epoch seconds), which a replay returns instead of taking a new one.', null=True),
        ),
        migrations.RunPython(dedupe_patches, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='patch',
            constraint=models.UniqueConstraint(fields=('session', 'global_rev', 'state'), name='patch_unique_rev_per_session_state'),
        ),
        migrations.AddField(
            model_name='task',
            name='parent_step',
            field=models.PositiveBigIntegerField(blank=True, help_text="The parent's step this child took (the AssignRequest's parent_step). A task's history is its events and patches by step, plus its children by parent_step. NULL for roots and for children of agents without numbering.", null=True),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(fields=('parent', 'parent_step'), name='task_unique_step_per_parent'),
        ),
    ]
