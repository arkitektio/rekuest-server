from authentikate.models import App, Organization
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.functions import Upper
from django_choices_field import TextChoicesField
from embeddings.models import EmbeddedDescriptionMixin, embedding_indexes

from facade import enums


class Action(EmbeddedDescriptionMixin, models.Model):
    """Actions are abstraction of RPC Tasks. They provide a common API to deal with creating tasks.

    Carries an embedding of its name + description (``EmbeddedDescriptionMixin``) so the
    catalog's ``search`` finds actions by what they do, not only by a substring of their name.

    See online Documentation"""

    app = models.ForeignKey(
        App,
        on_delete=models.CASCADE,
        related_name="actions",
        help_text="The app this action belongs to (actions are part of an app and are NOT associated only with a release)",
    )
    key = models.CharField(max_length=2000, help_text="A unique identifier for this action within the app")
    version = models.CharField(max_length=100, help_text="The version of this action (e.g. 1.0.0), this is used to differentiate if the underyling algorithm has changed, i.e we would expect different results for the same input")
    collections = models.ManyToManyField(
        "Collection",
        related_name="actions",
        help_text="The collections this Action belongs to",
    )
    pure = models.BooleanField(default=False, help_text="Is this function pure. e.g can we cache the result?")
    idempotent = models.BooleanField(
        default=False,
        help_text="Is this function idempotent. e.g can we run it multiple times without changing the data again ?",
    )
    allow_probe = models.BooleanField(
        default=False,
        help_text="May this action be invoked as a probe (zero persistence, no history/replay/recovery)? Declared by the action author; the probe mutation refuses actions without it.",
    )
    stateful = models.BooleanField(
        default=False,
        help_text="Is this function stateful. e.g does it inherently depend on or change state (think physical devices)?",
    )
    pinned_by = models.ManyToManyField(
        get_user_model(),
        related_name="pinned_actions",
        blank=True,
        help_text="The users that pinned this Actions",
    )
    kind = TextChoicesField(
        max_length=1000,
        choices_enum=enums.ActionKindChoices,
        default=enums.ActionKindChoices.FUNCTION.value,
        help_text="Function, generator? Will this function generate multiple results?",
    )
    port_groups = models.JSONField(default=list, help_text="Intercae that we use to interpret the meta data")
    name = models.CharField(max_length=1000, help_text="The cleartext name of this Action (e.g. 'Segment Image')", default="Unnamed Action")
    description = models.TextField(help_text="A description for the Action")
    scope = models.CharField(
        max_length=1000,
        default="GLOBAL",
        help_text="The scope of this Action. e.g. does the data it needs or produce live only in the scope of this Action or is it global or does it bridge data?",
    )
    is_test_for = models.ManyToManyField(
        "self",
        related_name="tests",
        blank=True,
        symmetrical=False,
        help_text="The users that have pinned the position",
    )
    protocols = models.ManyToManyField(
        "Protocol",
        related_name="actions",
        blank=True,
        help_text="The protocols this Action implements (e.g. Predicate)",
    )
    is_dev = models.BooleanField(default=False, help_text="Is this Action a development Action")

    hash = models.CharField(
        max_length=1000,
        help_text="The hash of the Action (completely unique)",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="actions",
        help_text="The organization this Action belongs to",
    )
    defined_at = models.DateTimeField(auto_created=True, auto_now_add=True)

    args = models.JSONField(default=list, help_text="Inputs for this Action")
    returns = models.JSONField(default=list, help_text="Outputs for this Action")

    arg_count = models.IntegerField(default=0, help_text="Pre-calculated number of root input ports")
    return_count = models.IntegerField(default=0, help_text="Pre-calculated number of root output ports")

    def __str__(self) -> str:
        return f"{self.name}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "app", "key", "version"],
                name="No multiple Actions with the same key and version in the same app for an organization allowed",
            )
        ]
        indexes = [
            # The matching engine's exact-match short circuits (facade.managers
            # _action_demand_clauses) and Action.objects.get(hash=...) all hit these columns;
            # the composite unique constraint above leads with organization+app and covers
            # none of them on its own.
            models.Index(fields=["hash"], name="action_hash_idx"),
            models.Index(fields=["organization", "key"], name="action_org_key_idx"),
            models.Index(fields=["name"], name="action_name_idx"),
            # The embedding healer's "any row not by the current model?" probe.
            *embedding_indexes("action"),
        ]


class BasePort(models.Model):
    """
    Abstract base class containing all the shared schema for Args and Returns.
    This guarantees Django writes to two physically separate tables.
    """

    index = models.IntegerField(help_text="The position of this port in the list")
    key = models.CharField(max_length=255, null=True, help_text="The local key (e.g. 'mask')")

    # The Semantic Materialized Path for O(1) nested execution lookups
    key_path = models.CharField(max_length=500, db_index=True, help_text="The full dot-notation path (e.g. 'options.advanced.mask')")

    kind = models.CharField(max_length=50, null=True, help_text="The structural kind (e.g. LIST, DICT, INT)")
    identifier = models.CharField(max_length=255, null=True, db_index=True, help_text="The macro-type (e.g. '@mikro/image')")
    dimension = models.CharField(max_length=255, null=True, blank=True, db_index=True, help_text="Canonical pint dimensionality for QUANTITY ports (the wiring-compatibility key)")

    # The ECS JSONPath string compiler output. TextField: the compiled predicate grows with
    # every descriptor (and every IN-list element), so a length cap would turn a large-but-valid
    # requires/provides declaration into a DataError at registration time.
    compiled_jsonpath = models.TextField(null=True, blank=True, help_text="PostgreSQL JSONPath string for micro-constraints")
    nullable = models.BooleanField(default=False)

    class Meta:
        abstract = True
        indexes = [
            # Composite index for instant payload resolution at execution time
            models.Index(fields=["action", "key_path"]),
            # Partial root-port indexes for the matcher: the correlated EXISTS clauses and the
            # force_* count subqueries all filter exactly on
            # (action_id, parent_id IS NULL, kind|nullable). Nested children are reached via the
            # FK index on parent with tiny fan-out, so only root rows are worth indexing.
            models.Index(fields=["action", "kind"], condition=models.Q(parent__isnull=True), name="%(class)s_root_kind_idx"),
            models.Index(fields=["action", "nullable"], condition=models.Q(parent__isnull=True), name="%(class)s_root_null_idx"),
            # Case-insensitive reverse lookup ("which actions use @mikro/image?") — the catalog
            # entities are lower-cased at registration while port identifiers keep original case.
            models.Index(Upper("identifier"), name="%(class)s_ident_upper_idx"),
        ]


class ArgPort(BasePort):
    """Inputs for the Action"""

    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="arg_ports")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children", help_text="If this port is nested inside a LIST or DICT")

    def __str__(self):
        return f"Arg: {self.key_path} ({self.identifier})"


class ReturnPort(BasePort):
    """Outputs for the Action"""

    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="return_ports")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children", help_text="If this port is nested inside a LIST or DICT")

    def __str__(self):
        return f"Return: {self.key_path} ({self.identifier})"


