from django.db import models

from facade import enums


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
    ports = models.JSONField(default=dict)
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
    value = models.JSONField(default=dict, help_text=" The current value of this state")
    created_at = models.DateTimeField(auto_now_add=True, help_text="Date this State was first ever written to")
    updated_at = models.DateTimeField(auto_now=True, help_text="Date this State was last updated")
    retention_policy = models.CharField(
        max_length=1000,
        choices=[(tag, tag.value) for tag in enums.RetentionPolicyChoices],
        default=enums.RetentionPolicyChoices.KEEP_ALL,
        help_text="The retention policy for this state (e.g. how many patches and snapshots should we keep for this state?)",
    )

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
    created_at = models.DateTimeField(auto_now_add=True, help_text="The time this session was created")
    updated_at = models.DateTimeField(auto_now=True, help_text="The time this session was last updated")
    active = models.BooleanField(default=True, help_text="Is this session active?")

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
    timestamp = models.DateTimeField(auto_now_add=True, help_text="The time this patch was created")
    global_rev = models.IntegerField(help_text="The current revision of the state in the global context (e.g. considering all patches that have been applied to this state)")
    task = models.ForeignKey("Task", on_delete=models.CASCADE, null=True, blank=True, help_text="The task that caused this patch (e.g. to be able to track changes by task)", related_name="patches")


class Snapshot(models.Model):
    state = models.ForeignKey(State, on_delete=models.CASCADE, related_name="snapshots")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="snapshots_created", null=True, blank=True)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="snapshots", null=True, blank=True)
    value = models.JSONField(help_text="The value of this snapshot (e.g. the value of the state at the time of the snapshot)")
    timestamp = models.DateTimeField(auto_now_add=True, help_text="The time this snapshot was created")
    global_rev = models.IntegerField(help_text="The revision of the state in the global context at the time of the snapshot (e.g. considering all patches that have been applied to this state)")
