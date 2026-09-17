import uuid

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django_choices_field import TextChoicesField

from facade import enums


class Task(models.Model):
    """A constant log of a tasks transition through finding a Action, Implementation and finally Pod , also a store for its results"""

    acted_on = ArrayField(base_field=models.CharField(max_length=1000), help_text="Which structures were acted on in this task", default=list)
    implementation = models.ForeignKey(
        "Implementation",
        on_delete=models.SET_NULL,
        help_text="Which implementation is the task currently mapped (can be reassigned)?",
        related_name="tasks",
        blank=True,
        null=True,
    )
    resolution = models.ForeignKey(
        "Resolution",
        on_delete=models.CASCADE,
        help_text="The resolution used for this task",
        related_name="tasks",
        blank=True,
        null=True,
    )
    action = models.ForeignKey("Action", on_delete=models.CASCADE, help_text="The action this was assigned to", related_name="tasks")
    ephemeral = models.BooleanField(
        default=False,
        help_text="Is this Task ephemeral (e.g. should it be deleted after its done or should it be kept for future reference)",
    )
    hooks = models.JSONField(
        default=list,
        help_text="hooks that are tight to the lifecycle of this task",
    )
    reference = models.CharField(
        max_length=1000,
        default=uuid.uuid4,
        help_text="The Unique identifier of this Task considering its parent",
    )
    dependency = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The reference of the dependency this task was assigned to (e.g. imagej)",
        default="general",
    )
    dependency_method = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The action of the dependency this task was assigned to (e.g. imagej.fft )",
    )
    capture = models.BooleanField(
        default=False,
        help_text="Should we capture the logs and events of this Task (e.g. for debugging or auditing purposes)?",
    )
    is_higher_order_child = models.BooleanField(
        default=False,
        help_text="Whether this task is the lower child of a higher-order wrapper — its yields/terminals unfold onto the wrapper. Lets the event hot path skip the parent lookup for ordinary tasks.",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="The Tasks parent (the one that created this (none if there is no parent))",
        related_name="children",
    )
    root = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="The Root parent (the one that was created by the user (none if this is the root))",
        related_name="all_children",
    )
    args = models.JSONField(blank=True, null=True, help_text="The Args", default=dict)
    args_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Canonical sha256 of the assign args (provenance canonicalization v1) — the replay-discovery key",
    )
    dependencies = models.JSONField(blank=True, null=True, help_text="The Args", default=dict)
    caller = models.ForeignKey(
        "Caller",
        on_delete=models.CASCADE,
        help_text="The caller (client/user/organization) that created this Task",
        null=True,
        blank=True,
        related_name="tasks",
    )
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This Task app",
        related_name="tasks",
    )
    latest_event_kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.TaskEventChoices,
        help_text="The latest Status of this Provision (transitioned by events)",
    )
    latest_instruct_kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.TaskInstructChoices,
        help_text="The latest Instruct of this Provision (transitioned by events)",
    )
    statusmessage = models.CharField(
        max_length=1000,
        help_text="Clear Text status of the Provision as for now",
        blank=True,
    )
    is_done = models.BooleanField(
        default=False,
        help_text="Is this Task done (e.g. has it been completed and resulted in an error?)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    revision = models.PositiveBigIntegerField(
        default=1,
        db_default=1,
        help_text="Monotonic per-task version, bumped by every write to this row (see ``save``). Carried in the task change feeds so a consumer can discard an update that arrives out of order — with several backends writing, channel-layer arrival order is not commit order.",
    )
    # --- Deadline bookkeeping -------------------------------------------------------------
    # Every deadline the server enforces lives in these columns rather than in a process-local
    # timer, so any backend (or a freshly restarted one) can act on it — see ``facade.reaper``.
    step = models.BooleanField(
        default=False,
        db_default=False,
        help_text="Whether this task was assigned in step mode — persisted so a redelivered Assign stays stepped.",
    )
    dispatched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the Assign was last handed to the agent's transport. NULL = never pushed (virtual higher-order wrapper, or the push failed) — the pickup watchdog owns the retry.",
    )
    dispatch_attempts = models.PositiveSmallIntegerField(
        default=0,
        db_default=0,
        help_text="How many times the Assign was dispatched (the pickup watchdog redelivers once, then fails the task).",
    )
    picked_up_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the executing agent first reported ANYTHING about this task. ``latest_event_kind`` cannot answer this: Progress/Log/Yield never move it, so a healthy running task still reads QUEUED.",
    )
    interrupt_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Deadline after which an unconfirmed cancel is escalated to an interrupt (auto_interrupt / control deadline).",
    )
    last_progress_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last Progress report of a physical-effect task — only stamped while the progress lease is enabled.",
    )

    def __str__(self):
        return f"{self.latest_event_kind} for {self.action_id}"

    def save(self, *args, **kwargs):
        """Every update bumps ``revision`` — atomically, in the database.

        ``F("revision") + 1`` rather than ``self.revision + 1``: the latter is only right when the
        instance was read under a row lock, and not every writer holds one. Django assigns the
        value the UPDATE returned back onto the instance *before* ``post_save`` fires, so the
        change-feed payload built there carries the fresh integer. ``updated_at`` rides along:
        ``auto_now`` is silently skipped whenever ``update_fields`` omits it, which is how nearly
        every task write is made — it used to be frozen at creation time.

        ``.update()`` sites bypass this on purpose: they touch only bookkeeping columns
        (``dispatched_at``, ``picked_up_at``, ``interrupt_at``, ``last_progress_at``) that no
        change feed carries.
        """
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or len(update_fields) > 0:
                self.revision = models.F("revision") + 1
                if update_fields is not None:
                    kwargs["update_fields"] = list({*update_fields, "revision", "updated_at"})
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            # THE assign idempotency guarantee. ``assign`` dedupes on the caller-supplied
            # reference with a read-then-create; with more than one backend two retries of the
            # same assign land on different processes at the same instant, both read "absent",
            # and without this both create a task and both dispatch it — the work runs twice.
            # NULL callers are distinct in Postgres, so caller-less rows are unaffected. The
            # constraint's own index serves the dedupe lookup.
            models.UniqueConstraint(fields=["caller", "reference"], name="task_unique_reference_per_caller"),
        ]
        indexes = [
            # The org-scoped ``tasks`` list. The org restriction lives on Agent (see
            # ``types.Task.get_queryset`` -> ``agent__organization``), so this is the Task-side
            # half of that join; it doubles as the ``agent`` filter and supplies the default
            # ``-created_at`` ordering and the created_before/after range without a sort node.
            models.Index(fields=["agent", "-created_at"], name="task_agent_created_idx"),
            # ``TaskFilter.state`` (latest_event_kind__in) org-wide, with the same ordering.
            # Leading with a ~16-value column is fine because the second column is the sort key.
            models.Index(fields=["latest_event_kind", "-created_at"], name="task_state_created_idx"),
            # ``TaskFilter.acted_on`` uses ``acted_on__overlap`` (``&&``) on an ArrayField. A btree
            # cannot answer overlap at all, so GIN is the only index type that avoids a seq scan.
            GinIndex(fields=["acted_on"], name="task_acted_on_gin_idx"),
            # ``queries.my_tasks``: filter(caller=, root__isnull=True, is_done=False)
            # .order_by("-created_at"). Both booleans are constants of that query, so they belong
            # in the condition — the partial index only ever holds the small live-root set.
            models.Index(
                fields=["caller", "-created_at"],
                condition=models.Q(root__isnull=True, is_done=False),
                name="task_my_root_open_idx",
            ),
            # ``queries.reusable_task_for``: args_hash is the selective key, the action ids come
            # from the Action-side hash/pure/organization predicate, ``-finished_at`` is the sort.
            models.Index(
                fields=["args_hash", "action", "-finished_at"],
                condition=models.Q(is_done=True, ephemeral=False, latest_event_kind=enums.TaskEventChoices.COMPLETED),
                name="task_replay_idx",
            ),
            # The pickup watchdog: open tasks no agent has reported on yet, by dispatch time.
            models.Index(
                fields=["dispatched_at"],
                condition=models.Q(is_done=False, picked_up_at__isnull=True),
                name="task_unpicked_idx",
            ),
            # The cancel→interrupt escalation sweep: only rows with a pending deadline.
            models.Index(
                fields=["interrupt_at"],
                condition=models.Q(is_done=False, interrupt_at__isnull=False),
                name="task_interrupt_due_idx",
            ),
            # The agent-disconnect and orphaned-executor sweeps (``ModelPersistBackend``,
            # ``facade.reaper``) all run filter(agent_id=, is_done=False). A partial index holds
            # only in-flight rows instead of walking that agent's whole history.
            models.Index(fields=["agent"], condition=models.Q(is_done=False), name="task_agent_open_idx"),
            # The retention sweep: filter(is_done=True, root__isnull=True, finished_at__lt=cutoff).
            # Partial on exactly those constants so it only ever holds terminal roots.
            models.Index(
                fields=["finished_at"],
                condition=models.Q(is_done=True, root__isnull=True),
                name="task_retention_idx",
            ),
        ]


class TaskEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    task = models.ForeignKey(
        Task,
        help_text="The task this log item belongs to",
        related_name="events",
        on_delete=models.CASCADE,
    )
    delegated_to = models.ForeignKey(
        Task,
        help_text="If this event was delegated to another task, which one?",
        related_name="delegated_events",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    returns = models.JSONField(
        help_text="The returns of the events (true for yield events)",
        null=True,
        blank=True,
    )
    progress = models.IntegerField(
        help_text="The progress of the task (0-100) (set for yield events)",
        null=True,
        blank=True,
    )
    message = models.CharField(max_length=30000, null=True, blank=True)
    # Status Field
    kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.TaskEventChoices,
        help_text="The event kind",
    )
    level = TextChoicesField(
        max_length=1000,
        choices_enum=enums.LogLevelChoices,
        help_text="The log level (LOG events)",
        null=True,
        blank=True,
    )


class TaskInstruct(models.Model):
    caller = models.ForeignKey(
        "Caller",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Which caller created this Instruction (if any?)",
        related_name="task_instructs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    task = models.ForeignKey(
        Task,
        help_text="The task this log item belongs to",
        related_name="instructs",
        on_delete=models.CASCADE,
    )
    # Status Field
    kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.TaskInstructChoices,
        help_text="The event kind",
    )


class AgentEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    agent = models.ForeignKey(
        "Agent",
        help_text="The agent",
        related_name="events",
        on_delete=models.CASCADE,
    )
    message = models.CharField(max_length=2000, null=True, blank=True)
    # Status Field
    kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.AgentEventChoices,
        help_text="The event kind",
    )
    level = TextChoicesField(
        max_length=1000,
        choices_enum=enums.LogLevelChoices,
        help_text="The event level",
        null=True,
        blank=True,
    )
