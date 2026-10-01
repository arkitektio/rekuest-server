from authentikate.models import Organization
from django.contrib.auth import get_user_model
from django.db import models
from django_choices_field import TextChoicesField
from rekuest_core.inputs.models import ActionDependencyInputModel, StateDependencyInputModel

from facade import enums


class Dependency(models.Model):
    """A Dependency

    Dependencies are predeclared dependencies for functions
    that will only ever rely on a specific set of functionality

    Functions that declare dependencies CANNOT dynamically
    reserve new functionality.


    """

    implementation = models.ForeignKey(
        "Implementation",
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
    )
    state_demands = models.JSONField(
        default=list,
        help_text="The state demands this dependency has to meet",
    )
    auto_resolvable = models.BooleanField(
        default=False,
        help_text="If this dependency is auto resolvable, the system will try to automatically bind any agent that the user can assign to this dependency. If False, the user will have to manually bind an agent to this dependency before it can be used.",
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

    optional = models.BooleanField(default=False, help_text="Is this dependency optional")
    description = models.TextField(null=True, blank=True, help_text="A description for this dependency")
    created_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    min_viable_instances = models.IntegerField(
        null=True,
        help_text="The minimal viable instance count for this dependency",
    )
    max_viable_instances = models.IntegerField(
        null=True,
        help_text="The maximal viable instance count for this dependency",
    )

    prefered_instances = models.IntegerField(
        null=True,
        help_text="The prefered instance count for this dependency",
    )

    def get_action_dependencies(self):
        return [ActionDependencyInputModel(**demand) for demand in self.action_demands]

    def get_state_dependencies(self):
        return [StateDependencyInputModel(**demand) for demand in self.state_demands]


class Resolution(models.Model):
    resolved_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    # Preset Logic
    name = models.CharField(max_length=200, null=True, blank=True, help_text="If set, this is a named preset (e.g. 'Standard Zeiss Config')")

    implementation = models.ForeignKey("Implementation", on_delete=models.CASCADE, related_name="resolutions", help_text="The specific Implementation that this tree is configuring dependencies for.")
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="created_resolutions",
        help_text="The user that created this Resolution",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="resolutions",
        help_text="The organization this Resolution belongs to",
    )


class ResolvedDependency(models.Model):
    # The Parent Scope

    resolution = models.ForeignKey(Resolution, on_delete=models.CASCADE, related_name="resolved_dependencies", help_text="The resolution scope this choice belongs to")
    dependency = models.ForeignKey(Dependency, on_delete=models.SET_NULL, null=True, blank=True, help_text="The dependency this choice is fulfilling")

    # The Requirement & The Choice
    key = models.CharField(max_length=2000)  # Matches Dependency.key
    resolution_key = models.CharField(max_length=2000, null=True, blank=True, help_text="An optional key to identify this resolution in the context of its parent resolution")
    implementation = models.ForeignKey(
        "Implementation",
        on_delete=models.PROTECT,  # Don't delete history if a tool is removed
        help_text="The tool chosen to fulfill the dependency",
    )

    # The Recursive Link (The Sub-Tree)
    # If the chosen implementation has dependencies itself, this is where they are configured and stored
    down_stream_resolution = models.ForeignKey(Resolution, on_delete=models.CASCADE, null=True, blank=True, related_name="upstream_dependencies", help_text="The configuration tree for THIS implementation's dependencies")


class Implementation(models.Model):
    """A Implementation is a conceptual implementation of A Action. It represents its implementation as well as its performance"""

    interface = models.CharField(max_length=1000, help_text="Interface (think Function)")
    action = models.ForeignKey(
        "Action",
        on_delete=models.CASCADE,
        help_text="The action this implementation is implementatig",
        related_name="implementations",
    )
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        help_text="The associated agent for this Implementation",
        related_name="implementations",
    )
    higher_order_for = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lower_order_implementations",
        help_text="If this implementation is a higher order implementation, this field links to the lower order implementation it is wrapping (the implementation will actually get the params, the implementation and the args of this)",
    )
    higher_order_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Projection config for a higher-order implementation (only meaningful when "
            "``higher_order_for`` is set). Describes how this wrapper's bound params + caller args + "
            "caller dependencies are remapped onto the wrapped (lower) implementation, and how the "
            "lower implementation's returns are unfolded back. Keys: ``bound`` (dict spread as the "
            "lower impl's named args), ``args_key`` (the lower arg port the remaining caller args are "
            "packed under), ``arg_map`` (explicit per-port arg remap), ``dependency_map`` (lower dep "
            "key -> source), ``return_map`` (lower return key -> wrapper return key)."
        ),
    )
    params = models.JSONField(default=dict, help_text="Params for this Implementation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracks = models.JSONField(default=list, help_text="A log of all the tasks that have been provisioned with this implementation, as well as their status and results")
    diagnostics = models.JSONField(default=list, help_text="Non-fatal registration findings (rekuest_core Diagnostic), e.g. validator/effect calls naming operations that neither the base catalog nor the definition's catalog provides. Replaced on every registration.")
    manipulates = models.ManyToManyField("State", help_text="Which states does this implementation manipulate?", related_name="manipulated_by")
    required_locks = models.ManyToManyField("Lock", related_name="required_by", blank=True, help_text="The agent's locks this implementation takes while it runs (declared as its `locks`).")
    needs_token = models.BooleanField(
        default=True,
        help_text="Whether Rekuest mints a signed provenance token when this implementation is assigned. Default true (provenance-by-default); false skips minting for trivial/internal tasks.",
    )
    provenance_audience = models.JSONField(
        null=True,
        blank=True,
        help_text="Optional declared audience (list of downstream service names) for the provenance token's `aud`. If null, the audience is derived at dispatch from the structures the assignment acts on.",
    )
    effects = TextChoicesField(
        choices_enum=enums.EffectsChoices,
        default=enums.EffectsChoices.UNKNOWN.value,
        help_text="What running this implementation again would do to the world. Informational: shown to whoever decides about a lost task.",
    )
    execution = TextChoicesField(
        choices_enum=enums.ExecutionChoices,
        default=enums.ExecutionChoices.PLAIN.value,
        help_text="How this implementation runs: a WORKFLOW may call other actions and is resumed from its journal when its agent dies.",
    )
    code_hash = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        help_text="A hash of the implementation's code. A workflow is only resumed by an implementation with the same hash.",
    )

    class Meta:
        permissions = [("providable", "Can provide this implementation")]
        constraints = [
            models.UniqueConstraint(
                fields=["interface", "agent"],
                name="A implementation has unique reachable versions for every action it trys to implement",
            ),
            models.UniqueConstraint(
                fields=["action", "agent"],
                name="An Agent can only have one implementation for the same Action",
            ),
        ]

    def __str__(self):
        return f"{self.action} implemented by {self.agent}"
