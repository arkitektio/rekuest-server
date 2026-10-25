from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.functions import Now
from django_choices_field import TextChoicesField

from authentikate.models import App

from rekuest_core.inputs.models import ActionDependencyInputModel, StateDependencyInputModel

from facade import enums

from .action import BasePort, port_indexes


class Dashboard(models.Model):
    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    placements: "models.Manager[DashboardPlacement]"
    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="dashboards",
        help_text="The organization this Dashboard belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=2000)


class Blok(models.Model):
    """A UI template. Either custom (``app`` NULL: a user wrote it, it is unique by name in its
    organization and lives until deleted) or provided (``app`` set: an agent of that app declared
    it at registration). A provided blok is content-addressed by ``declaration_hash`` and shared
    by every agent of the app declaring the same content; each of them holds it through its own
    ``MaterializedBlok`` slot, and takt deletes the blok when the last slot lets go. Users cannot
    edit or materialize a provided blok, they fork it into a custom one."""

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    app_id: int | None
    organization_id: int
    head_id: int | None
    dependencies: "models.Manager[BlokDependency]"
    arg_ports: "models.Manager[BlokArgPort]"
    revisions: "models.Manager[BlokRevision]"
    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="bloks",
        help_text="The organization this Blok belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=1000)
    description = models.TextField(null=True, blank=True)
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="bloks",
        help_text="The user that created this Blok",
    )
    catalog = models.ForeignKey(
        "UICatalog",
        on_delete=models.CASCADE,
        related_name="bloks",
        help_text="The catalog this Blok belongs to",
    )
    components = models.JSONField(help_text="The UI schema for this Blok", default=list, db_default=[])
    demo_state = models.JSONField(help_text="The initial state for this Blok (to display in the ui a fake version)", default=dict, db_default={})
    context = models.CharField(max_length=16, default="HTML", db_default="HTML", help_text="The renderer for this Blok's root (rekuest_core ComponentContext): HTML, or SCENE for a full 3D view.")
    for_model = models.CharField(max_length=71, null=True, blank=True, db_index=True, help_text="The model reference ('sha256:...') this Blok decorates, if any. Viewers showing that model offer the Blok at `for_node`.")
    for_node = models.CharField(max_length=1000, null=True, blank=True, help_text="The node of `for_model` this Blok attaches to; the model's root when NULL.")
    args = models.JSONField(default=list, db_default=[], help_text="The items this Blok is opened for: its declared arg ports (rekuest_core ArgPortInput), read under `@args.<key>`. Matched through its BlokArgPort rows.")
    returns = models.JSONField(default=list, db_default=[], help_text="What a person answers through this Blok when a task asks with it (rekuest_core ReturnPortInput): the values `blok.answer` hands back.")
    services = models.JSONField(default=list, db_default=[], help_text="The backend services this Blok reads from (rekuest_core BlokServiceInput: key, service, version, schema_hash), each frozen at the version it was written against.")
    persist = models.JSONField(default=list, db_default=[], help_text="The locals a renderer may keep between openings, on the device (rekuest_core BlokPersistInput: key, description). Only the declaration is stored here: the values never reach a server.")
    owner_task = models.ForeignKey(
        "Task",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="owned_bloks",
        help_text="The task that made this Blok from its BSX to show it (ask or inform). It is never listed and goes with its task. NULL for every other Blok.",
    )
    diagnostics = models.JSONField(default=list, help_text="Non-fatal registration findings (rekuest_core Diagnostic), e.g. manifest util calls naming operations that neither the base catalog nor this blok's catalog provides. Replaced on every write.", db_default=[])
    app = models.ForeignKey(
        App,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="provided_bloks",
        help_text="The app whose agents provide this Blok. NULL for a custom Blok a user made.",
    )
    declaration_hash = models.CharField(max_length=64, null=True, blank=True, help_text="sha256 of the declared content of a provided Blok (not the rendering `hash` the API computes); NULL for a custom one.")
    held_by_action = models.BooleanField(default=False, db_default=False, help_text="Whether this provided Blok is an action's own (`ActionBlok`): declared inline with the action, it has no name of its own and is never listed, materialized or edited. takt deletes it once no action holds it.")
    origin = models.CharField(max_length=2000, null=True, blank=True, help_text="Where a custom Blok was installed from, e.g. kabinet's `github:<owner>/<repo>:<branch>#<key>`; NULL for one made here. Installing from the same origin again updates this row.")
    origin_hash = models.CharField(max_length=100, null=True, blank=True, help_text="The hash its origin gave what it draws when it was installed: an origin whose hash differs has an update.")
    head = models.ForeignKey(
        "BlokRevision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The revision this custom Blok's columns and dependency rows are a copy of. NULL for a provided Blok, which its declaration hash versions instead.",
    )

    class Meta:
        # createBlok upserts a custom blok on (organization, name); registration upserts a provided
        # one on its content, so v1 and v2 of an app never overwrite each other.
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], condition=models.Q(app__isnull=True, owner_task__isnull=True), name="unique_blok_name_per_organization"),
            models.UniqueConstraint(fields=["organization", "app", "name", "declaration_hash"], condition=models.Q(app__isnull=False), name="blok_unique_provided"),
            models.UniqueConstraint(fields=["organization", "origin"], condition=models.Q(app__isnull=True, origin__isnull=False), name="unique_blok_origin_per_organization"),
        ]


class ActionBlok(models.Model):
    """An action's own blok for one context: how a client shows a run of the action there.

    Declared inline with the action at registration and not part of its hash. The blok is a
    provided ``Blok`` with ``held_by_action`` set, content-addressed and shared by every action
    of the app declaring the same; takt writes these rows on every registration (the latest one
    wins, like protocol hints) and collects a blok once no action holds it. Its dependencies are
    never stored: the blok names them by the action's dependency keys.
    """

    action_id: int
    blok_id: int

    action = models.ForeignKey("Action", on_delete=models.CASCADE, related_name="bloks", help_text="The action this blok shows runs of.")
    context = models.CharField(max_length=16, help_text="Where a client shows a run with it (rekuest_core ComponentContext): HTML, SCENE or OVERLAY. One blok per context.")
    blok = models.ForeignKey(Blok, on_delete=models.CASCADE, related_name="action_bloks", help_text="The blok: a provided one, held by its actions only. takt collects it once no action holds it; going with its catalog or creator, it takes this row along.")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["action", "context"], name="unique_action_blok_context")]


class BlokRevision(models.Model):
    """One state of a custom Blok, never changed once written.

    Every write to a custom blok commits a revision and copies it onto the ``Blok`` (its ``head``),
    so everything that reads a blok keeps reading the row while history only grows: an undo is a
    new revision with an old one's content. A write names the revision it was made from, and one
    made from anything but the head is refused, so a change made meanwhile is never lost.
    """

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    blok_id: int
    parent_id: int | None
    catalog_id: int
    task_id: int | None

    blok = models.ForeignKey(Blok, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField(help_text="The place of this revision in its blok's history, from 1.")
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children", help_text="The revision this one was made from; NULL for the first.")
    components = models.JSONField(default=list, db_default=[], help_text="The component tree of this revision.")
    demo_state = models.JSONField(default=dict, db_default={}, help_text="The local state of this revision.")
    context = models.CharField(max_length=16, default="HTML", db_default="HTML", help_text="The renderer for this revision's root (rekuest_core ComponentContext).")
    catalog = models.ForeignKey("UICatalog", on_delete=models.CASCADE, related_name="blok_revisions", help_text="The catalog this revision renders against.")
    dependencies = models.JSONField(default=list, db_default=[], help_text="The agent dependencies of this revision (rekuest_core AgentDependencyInput), as the blok's dependency rows held them.")
    args = models.JSONField(default=list, db_default=[], help_text="The args of this revision (rekuest_core ArgPortInput): the items the blok is opened for.")
    returns = models.JSONField(default=list, db_default=[], help_text="The returns of this revision (rekuest_core ReturnPortInput): what a person answers through the blok.")
    services = models.JSONField(default=list, db_default=[], help_text="The services of this revision (rekuest_core BlokServiceInput): what its queries read from, at the version they were written against.")
    persist = models.JSONField(default=list, db_default=[], help_text="The persisted locals of this revision (rekuest_core BlokPersistInput): what a renderer may keep between openings, on the device.")
    content_hash = models.CharField(max_length=64, help_text="sha256 of everything above: two revisions with the same hash are the same blok.")
    author = models.ForeignKey(get_user_model(), on_delete=models.SET_NULL, null=True, blank=True, related_name="blok_revisions", help_text="The user this revision was written as.")
    task = models.ForeignKey("Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="blok_revisions", help_text="The task that wrote this revision, e.g. the assistant turn that designed it.")
    message = models.TextField(null=True, blank=True, help_text="What changed and why, in a sentence.")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["blok", "number"], name="unique_revision_number_per_blok")]


class BlokArgPort(BasePort):
    """A Blok's arg port as a row, so ``demands`` find the bloks an item opens in the way they find actions."""

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    blok_id: int
    parent_id: int | None

    blok = models.ForeignKey(Blok, on_delete=models.CASCADE, related_name="arg_ports")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children", help_text="If this port is nested inside a LIST or DICT")

    class Meta:
        indexes = port_indexes("blok")

    def __str__(self) -> str:
        return f"Blok arg: {self.key_path} ({self.identifier})"


class BlokDependency(models.Model):
    """A Dependency

    Dependencies are predeclared dependencies for functions
    that will only ever rely on a specific set of functionality

    Functions that declare dependencies CANNOT dynamically
    reserve new functionality.


    """

    blok = models.ForeignKey(
        "Blok",
        on_delete=models.CASCADE,
        help_text="The implementation that has this dependency",
        related_name="dependencies",
    )
    key = models.CharField(
        max_length=2000,
        help_text="A reference for this dependency",
    )
    action_demands = models.JSONField(
        default=list,
        help_text="The action demands this dependency has to meet",
        db_default=[],
    )
    state_demands = models.JSONField(
        default=list,
        help_text="The state demands this dependency has to meet",
        db_default=[],
    )
    auto_resolvable = models.BooleanField(
        default=False,
        help_text="If this dependency is auto resolvable, the system will try to automatically bind any agent that the user can assign to this dependency. If False, the user will have to manually bind an agent to this dependency before it can be used.",
        db_default=False,
    )
    app_filter = models.CharField(
        max_length=2000,
        null=True,
        blank=True,
        help_text="If set, only Agents of this app will be able to be assigned to this dependency.",
    )
    version_filter = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="If set, only Agents of this version will be able to be assigned to this dependency",
    )
    optional = models.BooleanField(default=False, help_text="Is this dependency optional", db_default=False)
    description = models.TextField(null=True, blank=True, help_text="A description for this dependency")
    created_at = models.DateTimeField(auto_created=True, auto_now_add=True, db_default=Now())
    min_viable_instances = models.IntegerField(
        null=True,
        help_text="The minimal viable instance count for this dependency",
    )
    max_viable_instances = models.IntegerField(
        null=True,
        help_text="The maximal viable instance count for this dependency",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["blok", "key"], name="unique_dependency_key_per_blok")]

    def get_action_dependencies(self) -> list[ActionDependencyInputModel]:
        return [ActionDependencyInputModel(**demand) for demand in self.action_demands]

    def get_state_dependencies(self) -> list[StateDependencyInputModel]:
        return [StateDependencyInputModel(**demand) for demand in self.state_demands]


class MaterializedBlok(models.Model):
    """A Blok Implementation is a specific implementation of a Blok"""

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    blok_id: int
    declared_by_id: int | None
    pinned_revision_id: int | None
    agent_mappings: "models.Manager[BlokAgentMapping]"

    blok = models.ForeignKey(Blok, on_delete=models.CASCADE, related_name="materialized_bloks")
    declared_by = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="declared_materialized_bloks",
        help_text="The agent whose registration auto-materialized this blok, if any. NULL means a user created it by hand. An auto-materialization is meaningless without its agent, hence CASCADE.",
    )
    name = models.CharField(max_length=1000, help_text="The name of this Blok Implementation")
    description = models.TextField(help_text="A description for this Blok Implementation")
    pinned_revision = models.ForeignKey(
        BlokRevision,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pinned_by",
        help_text="The revision this materialization stays at. NULL follows the blok's head, which is what a materialization does unless pinned.",
    )
    is_default = models.BooleanField(default=False, db_default=False, help_text="Whether this is its declaring agent's default Blok for the Blok's context. At most one per agent and context: declaring a new default clears the previous one.")
    scope = TextChoicesField(  # pyright: ignore[reportCallIssue]  django-choices-field's stub predates db_default
        max_length=1000,
        choices_enum=enums.MaterializationScopeChoices,  # pyright: ignore[reportCallIssue]  django-choices-field's stub predates db_default
        default=enums.MaterializationScopeChoices.DASHBOARD.value,
        db_default="DASHBOARD",
        help_text="Where it lives: DASHBOARD ones are listed; a MESSAGE one was made for one chat message, a TASK one for the task that showed it. Those are reached only by their id.",
    )
    owner_task = models.ForeignKey(
        "Task",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shown_bloks",
        help_text="The task that showed this materialization (scope TASK): it goes with its task.",
    )
    task_role = models.CharField(max_length=16, null=True, blank=True, choices=enums.TaskBlokRoleChoices.choices, help_text="Why its task showed it: ASK (the task holds until a person answers through it) or INFORM.")
    args = models.JSONField(default=dict, db_default={}, help_text="What it is opened on, by arg key (read under `@args.<key>`). Empty for a materialization opened by whoever shows it.")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        constraints = [
            # A blok may be materialized many times by hand, but each agent that DECLARES one
            # gets exactly one row. Registration used to upsert on ``blok`` alone: two agents
            # declaring the same blok fought over one row, and a single hand-made row made every
            # registration raise ``MultipleObjectsReturned`` from then on.
            models.UniqueConstraint(
                fields=["blok", "declared_by"],
                condition=models.Q(declared_by__isnull=False),
                name="mblok_unique_declaration_per_agent",
            ),
            # An agent's slot for one declared blok key: re-registering changed content re-points
            # the slot to the new template and keeps its id, so placements survive.
            models.UniqueConstraint(
                fields=["declared_by", "name"],
                condition=models.Q(declared_by__isnull=False),
                name="mblok_unique_slot_per_agent",
            ),
        ]


class DashboardPlacement(models.Model):
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="placements")
    blok = models.ForeignKey(MaterializedBlok, on_delete=models.CASCADE, related_name="dashboard_placements")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


class BlokAgentMapping(models.Model):
    """An Agent Mapping is a mapping between an Agent and a Blok Implementation"""

    # Declared for the type checker: Django adds the foreign key's id column without saying so.
    agent_id: int

    key = models.CharField(max_length=1000, help_text="The reference of the dependency this mapping is for (e.g. imagej)", default="general", db_default="general")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="agent_mappings")
    materialized_blok = models.ForeignKey(
        MaterializedBlok,
        on_delete=models.CASCADE,
        related_name="agent_mappings",
    )
    dependency = models.ForeignKey(
        BlokDependency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The dependency this mapping is fulfilling (if any)",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        # Prevents mapping 'stage_dep' to two different agents inside the same materialized instance
        constraints = [models.UniqueConstraint(fields=["materialized_blok", "key"], name="unique_dependency_per_materialized_blok")]
