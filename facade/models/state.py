
from django.db.models.functions import Now
from django.db import models



class StateDefinition(models.Model):
    """A state definition is an abstract representation of a state and describes
    the ports (datatypes) that a state can have. States also follow the
    port schema

    State definitions do not belong directly to an agent, but are
    used by agents to describe the states they can have.

    The concept is closing linked to the concepot of a "Action", but
    representing state, rather than a function.


    """

    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="state_definitions",
        help_text="The organization this StateDefinition belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=2000)
    hash = models.CharField(max_length=2000, help_text="sha256 over the ports; unique per organization")
    ports = models.JSONField(default=dict, db_default={})
    description = models.CharField(max_length=2000)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "hash"], name="unique_state_definition_hash_per_organization")]


class State(models.Model):
    """A state is a representation of the current state of a action.

    States always follow a schema and represent the current
    state of a action. States are used to represent the current

    """

    definition = models.ForeignKey(StateDefinition, on_delete=models.CASCADE, related_name="states")
    interface = models.CharField(
        max_length=1000,
        help_text="The interface this state is for (e.g. Function)",
    )
    key = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The stable identity key of this state, matched by state demands (defaults to the interface at registration)",
    )
    app_identifier = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The identifier of the app providing this state (defaults to the owning agent's app identifier at registration)",
    )
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="states")
    created_at = models.DateTimeField(auto_now_add=True, help_text="Date this State was first ever written to", db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, help_text="Date this State was last updated", db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["interface", "agent"],
                name="No multiple States for same Agent and Schema allowed",
            )
        ]


class Session(models.Model):
    """A Session is a representation of a user session. Sessions are used to represent the current session of a user and can be used to track the changes that happen to a state over time. They are stored as a log of changes to a state and can be used to reconstruct the state at any point in time."""

    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="sessions")
    session_id = models.CharField(max_length=1000, help_text="The unique identifier for this session")
    created_at = models.DateTimeField(auto_now_add=True, help_text="The time this session was created", db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, help_text="The time this session was last updated", db_default=Now())
    projected_pos = models.PositiveBigIntegerField(
        default=0,
        help_text="Every numbered frame of this session up to this position is handled: a resend at or below it is skipped, and JOURNAL_ACK claims it (docs/design/journal.md).",
        db_default=0,
    )
    claimed_pos = models.PositiveBigIntegerField(
        default=0,
        help_text="The position a backend is projecting right now (claimed_pos = projected_pos + 1 while one is in flight, else equal). Another backend waits for it, or takes it over once claimed_at is stale.",
        db_default=0,
    )
    claimed_at = models.DateTimeField(null=True, blank=True, help_text="When claimed_pos was claimed")

    class Meta:
        constraints = [
            # ``aget_or_create(agent, session_id)`` runs from three handlers on whichever backend
            # holds the socket. Two rows for one logical session split its patch/snapshot log, and
            # state reconstruction then silently returns half a history (``logic.py`` picks the
            # newest row). The constraint makes ``get_or_create`` retry into the winner instead.
            models.UniqueConstraint(fields=["agent", "session_id"], name="session_unique_id_per_agent"),
        ]


class Patch(models.Model):
    """A Patch is a representation of a change to a state. Patches are used to represent the changes that happen to a state over time. They are stored as a log of changes to a state and can be used to reconstruct the state at any point in time."""

    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="patches")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="patches_created", null=True, blank=True)
    interface = models.CharField(max_length=1000, help_text="The interface of the state in the agent")
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="patches", null=True, blank=True)
    op = models.CharField(max_length=1000, help_text="The operation of this patch (e.g. add, remove, replace)")
    path = models.CharField(max_length=1000, help_text="The path of this patch (e.g. the path to the value that is being changed)")
    value = models.JSONField(help_text="The value of this patch (e.g. the new value that is being set)")
    timestamp = models.DateTimeField(auto_now_add=True, help_text="The time this patch was created", db_default=Now())
    global_rev = models.IntegerField(help_text="The current revision of the state in the global context (e.g. considering all patches that have been applied to this state)")
    task = models.ForeignKey("Task", on_delete=models.CASCADE, null=True, blank=True, help_text="The task that caused this patch (e.g. to be able to track changes by task)", related_name="patches")
    old_value = models.JSONField(null=True, blank=True, help_text="The value the patch replaced, when the agent reported it (debugging and tracing only; never used to reconstruct state)")
    agent_pos = models.PositiveBigIntegerField(null=True, blank=True, help_text="The session position (pos) of the frame that carried this patch. NULL for agents without numbering.")
    agent_ts = models.DateTimeField(null=True, blank=True, help_text="When the agent recorded the patch (the frame's agent_ts). NULL for agents without numbering.")
    step = models.PositiveBigIntegerField(null=True, blank=True, help_text="The changing task's step (the frame's task_step). NULL for agents without numbering and patches outside a task.")

    class Meta:
        constraints = [
            # One patch per revision of a state in a session: ``global_rev`` is bumped once per
            # patch by the agent, so a second row is a resend. Without it a resent patch was
            # applied twice on reconstruction. NULL sessions stay distinct in Postgres.
            models.UniqueConstraint(fields=["session", "global_rev", "state"], name="patch_unique_rev_per_session_state"),
        ]


class Snapshot(models.Model):
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="snapshots")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="snapshots_created", null=True, blank=True)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="snapshots", null=True, blank=True)
    value = models.JSONField(help_text="The value of this snapshot (e.g. the value of the state at the time of the snapshot)")
    timestamp = models.DateTimeField(auto_now_add=True, help_text="The time this snapshot was created", db_default=Now())
    global_rev = models.IntegerField(help_text="The revision of the state in the global context at the time of the snapshot (e.g. considering all patches that have been applied to this state)")
