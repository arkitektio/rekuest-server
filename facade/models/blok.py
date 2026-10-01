from django.contrib.auth import get_user_model
from django.db.models.functions import Now
from django.db import models
from rekuest_core.inputs.models import ActionDependencyInputModel, StateDependencyInputModel



class Dashboard(models.Model):
    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="dashboards",
        help_text="The organization this Dashboard belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=2000)


class Blok(models.Model):
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
    diagnostics = models.JSONField(default=list, help_text="Non-fatal registration findings (rekuest_core Diagnostic), e.g. manifest util calls naming operations that neither the base catalog nor this blok's catalog provides. Replaced on every write.", db_default=[])

    class Meta:
        # Every write path upserts on (organization, name); the constraint makes that upsert safe.
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="unique_blok_name_per_organization")]


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

    def get_action_dependencies(self):
        return [ActionDependencyInputModel(**demand) for demand in self.action_demands]

    def get_state_dependencies(self):
        return [StateDependencyInputModel(**demand) for demand in self.state_demands]


class MaterializedBlok(models.Model):
    """A Blok Implementation is a specific implementation of a Blok"""

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
        ]


class DashboardPlacement(models.Model):
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="placements")
    blok = models.ForeignKey(MaterializedBlok, on_delete=models.CASCADE, related_name="dashboard_placements")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


class BlokAgentMapping(models.Model):
    """An Agent Mapping is a mapping between an Agent and a Blok Implementation"""

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
