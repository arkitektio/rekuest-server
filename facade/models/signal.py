"""Signals and triggers: a service says what happened, a user's trigger decides what runs.

A **Signal** is one event a service announced (``POST /agi/signal/<service>``): an object of a
structure (``@mikro/arraydataset:42``) was created, updated or deleted, with the object's
descriptors and — when the service was called inside a task and forwarded that task's
provenance token — the task that caused it. rekuest verifies that token against its own key, so
``causing_task`` is a fact, never a service's claim.

A **Trigger** is a user's rule over signals of one kind and structure identifier: when the
signal's descriptors satisfy the trigger's conditions AND the target port's own ``requires``,
assign the action with the object in that port. The reaper's ``fire_triggers`` does it, once per
trigger × signal (the run's reference is ``trigger:<id>:<signal>``). A run caused by a task is
that task's child, so it joins the causing tree — lineage, cancellation, retention, provenance.
"""

from typing import TYPE_CHECKING

from django.db import models
from django.db.models.functions import Now

from facade import enums

if TYPE_CHECKING:
    from facade.models.task import Task


class Signal(models.Model):
    """One event a service announced. Best-effort: nothing redelivers a signal that never arrived."""

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    tasks: "models.Manager[Task]"

    service = models.CharField(max_length=200, help_text="The service that sent it (its `rekuest.services` name)")
    signal_id = models.CharField(max_length=200, help_text="The service's id for the signal; a resend with the same id is a no-op")
    kind = models.CharField(max_length=20, choices=enums.SignalKindChoices.choices, help_text="What happened to the object")
    identifier = models.CharField(max_length=1000, help_text="The object's structure identifier, e.g. @mikro/arraydataset")
    object = models.CharField(max_length=1000, help_text="The object's id within its structure")
    organization = models.ForeignKey("authentikate.Organization", on_delete=models.CASCADE, related_name="signals", help_text="The organization the object belongs to")
    descriptors = models.JSONField(default=dict, blank=True, help_text="The object's descriptors (flat key → value), matched against triggers' conditions and ports' `requires`", db_default={})
    causing_task = models.ForeignKey(
        "Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="caused_signals",
        help_text="The task the object was created in — from a provenance token rekuest verified, never from the service's word",
    )
    occurred_at = models.DateTimeField(null=True, blank=True, help_text="When it happened, per the service")
    received_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    processed_at = models.DateTimeField(null=True, blank=True, help_text="When `fire_triggers` matched it; null = not yet")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["service", "signal_id"], name="signal_unique_per_service")]
        indexes = [
            models.Index(fields=["received_at"], condition=models.Q(processed_at__isnull=True), name="signal_unprocessed_idx"),
            models.Index(fields=["organization", "-received_at"], name="signal_org_received_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.identifier}:{self.object} from {self.service}"


class Trigger(models.Model):
    """A user's rule: on signals of this kind and structure, whose descriptors match, run this action."""

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    action_id: int
    agent_id: int | None
    caller_id: int
    tasks: "models.Manager[Task]"

    name = models.CharField(max_length=200, help_text="A human-readable name for the trigger")
    caller = models.ForeignKey(
        "Caller",
        on_delete=models.CASCADE,
        related_name="triggers",
        help_text="The owner: runs are assigned as this identity, its organization scopes the trigger and the signals it sees",
    )
    enabled = models.BooleanField(default=True, help_text="A disabled trigger fires nothing", db_default=True)
    kind = models.CharField(max_length=20, choices=enums.SignalKindChoices.choices, help_text="The signal kind it reacts to")
    identifier = models.CharField(max_length=1000, help_text="The structure identifier it reacts to, e.g. @mikro/arraydataset")
    conditions = models.JSONField(default=list, blank=True, help_text="Extra descriptor conditions (requires-style: key, operator, value)", db_default=[])
    compiled_jsonpath = models.TextField(null=True, blank=True, help_text="`conditions` compiled to a PostgreSQL JSONPath predicate; null = no extra conditions")
    action = models.ForeignKey("Action", on_delete=models.CASCADE, related_name="triggers", help_text="The action every run assigns")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, null=True, blank=True, related_name="triggers", help_text="Pin runs to this agent (with `interface`)")
    interface = models.CharField(max_length=1000, null=True, blank=True, help_text="The implementation interface on the pinned agent")
    port = models.CharField(max_length=1000, help_text="The STRUCTURE arg that receives the signalled object")
    args = models.JSONField(default=dict, blank=True, help_text="The other args of every run", db_default={})
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    consecutive_failures = models.PositiveIntegerField(default=0, help_text="Firings in a row that could not create a run", db_default=0)
    last_error = models.TextField(null=True, blank=True, help_text="Why the last firing did not create a run")
    description = models.TextField(null=True, blank=True, help_text="What the rule is for")
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Nothing fires after this moment; null = never ends")
    max_runs = models.PositiveIntegerField(null=True, blank=True, help_text="Nothing fires once it created this many; null = no limit")
    run_count = models.PositiveIntegerField(default=0, help_text="Runs it created, counted by takt (retention deletes old runs, this stays)", db_default=0)
    last_fired_at = models.DateTimeField(null=True, blank=True, help_text="When it last created a run")
    last_error_at = models.DateTimeField(null=True, blank=True, help_text="When `last_error` was written")
    wiregram = models.ForeignKey("Wiregram", on_delete=models.CASCADE, null=True, blank=True, related_name="triggers", help_text="The wiregram that owns this rule, if it was imported with one")
    wire_key = models.CharField(max_length=200, null=True, blank=True, help_text="What the wiregram's document calls this rule")
    debounce_seconds = models.PositiveIntegerField(null=True, blank=True, help_text="Fire at most once per object within this many seconds (the first signal fires, later ones are rejected); null = every signal")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["wiregram", "wire_key"], condition=models.Q(wiregram__isnull=False), name="trigger_unique_wire_key"),
        ]
        indexes = [
            models.Index(fields=["kind", "identifier"], condition=models.Q(enabled=True), name="trigger_enabled_match_idx"),
            models.Index(fields=["caller", "-created_at"], name="trigger_caller_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} (on {self.kind} {self.identifier})"


class Firing(models.Model):
    """What became of one trigger for one signal: the firing log.

    takt writes one row for every enabled trigger that listens for a signal's kind and structure
    in its organization, whether it ran or not, so a signal that caused nothing can be told from
    one nobody listened for (that one has no firings at all), and a rule that never fires says why.
    A row lives as long as its signal (signal retention removes both).
    """

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    signal_id: int
    trigger_id: int

    signal = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name="firings", help_text="The signal")
    trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, related_name="firings", help_text="The trigger that was tried on it")
    outcome = models.CharField(max_length=20, choices=enums.FiringOutcomeChoices.choices, help_text="FIRED: a run was created. REJECTED: the trigger did not apply. FAILED: creating the run failed.")
    reason = models.TextField(null=True, blank=True, help_text="Why it was rejected or failed; for a replay, that it was one")
    task = models.ForeignKey("Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="firings", help_text="The run it created, while that run exists")
    replay = models.BooleanField(default=False, db_default=False, help_text="Fired by hand on a stored signal, not by the signal arriving")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            # Once per trigger × signal, like the run's reference; a replay is a firing of its own.
            models.UniqueConstraint(fields=["signal", "trigger"], condition=models.Q(replay=False), name="firing_unique_per_signal_trigger"),
        ]
        indexes = [
            models.Index(fields=["trigger", "-created_at"], name="firing_trigger_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.outcome}: trigger {self.trigger_id} on signal {self.signal_id}"


class SignalDeclaration(models.Model):
    """A signal a service says it emits, read from its manifest by provisioning.

    Hub-wide, not organization-scoped: a service emits them about every organization's objects.
    Triggers are checked against these — the kind and
    identifier exist, the condition keys are ones the service sends — and a UI lists them.
    """

    service = models.ForeignKey("Service", on_delete=models.CASCADE, related_name="signals", help_text="The service that emits it")
    identifier = models.CharField(max_length=1000, help_text="The structure identifier of the objects signalled")
    kind = models.CharField(max_length=20, choices=enums.SignalKindChoices.choices, help_text="What happens to them")
    descriptor_keys = models.JSONField(default=list, blank=True, help_text="The descriptor keys each signal carries", db_default=[])
    description = models.TextField(null=True, blank=True, help_text="What the service says about the signal")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["service", "identifier", "kind"], name="signal_declaration_unique")]

    def __str__(self) -> str:
        return f"{self.service} emits {self.kind} {self.identifier}"
