from authentikate.models import Client, Organization
from django.db.models.functions import Now
from django.contrib.auth import get_user_model
from django.db import models

from rekuest_core.inputs.models import CatalogComponentInputModel, CatalogOperationInputModel, WidgetDefaultInputModel


class Collection(models.Model):
    """A collection is a group of actions that are related to each other.

    You can put actions into a collection to group them together, and
    app developers can specify which collection a action belongs to
    by default.

    Collections should remain domain specific.

    Example collections are:
    - Segmentation
    - Classification
    - Image Processing


    """

    name = models.CharField(max_length=1000, help_text="The name of this Collection, unique within its organization")
    description = models.TextField(help_text="A description for the Collection")
    defined_at = models.DateTimeField(
        auto_created=True,
        auto_now_add=True,
        help_text="Date this Collection was created",
        db_default=Now(),
    )
    updated_at = models.DateTimeField(auto_now=True, help_text="Date this Collection was last updated", db_default=Now())
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="collections",
        help_text="The user that created this Collection",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="collections",
        help_text="The Organization this Collection belongs to",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="collection_unique_name_per_organization")]


class Protocol(models.Model):
    """Protocols are ways to describe the input and output relations
    of a action. When a new action is created the
    interference module will try to infer the protocol of the action
    and assign it to the action. This is done by looking at the
    input and output types of the action and matching them to the
    protocols that are defined by your installed inference modules.

    """

    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="protocols",
        help_text="The organization this Protocol belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=1000, help_text="The name of this Protocol, unique within its organization")
    description = models.TextField(help_text="A description for the Protocol")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="protocol_unique_name_per_organization")]

    def __str__(self) -> str:
        return self.name


class Module(models.Model):
    """A module: an app's own, unversioned UI extras (components and operations), registered by
    the app (the Orkestrator registers ``orkestrator``) with the spec's versioned catalogs it
    implements in full. A blok names at most one, as its ``module``; definitions name the
    modules their effect and validator calls use (``DefinitionInput.catalogs``, base@1).
    """

    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="modules",
        help_text="The organization this Module belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=1000)
    description = models.TextField(null=True, blank=True, help_text="A description of this module")
    components = models.JSONField(
        default=list,
        help_text="Registered component specs (rekuest_core CatalogComponent): the names a ComponentNode.component may use and the props each accepts.",
        db_default=[],
    )
    operations = models.JSONField(
        default=list,
        help_text="Registered operation specs (rekuest_core CatalogOperation): the names a UtilCall.operation may use, their arguments and return kind.",
        db_default=[],
    )
    widget_defaults = models.JSONField(
        default=list,
        help_text="Default widgets per port kind and/or structure identifier (rekuest_core WidgetDefault), applied by the UI to ports that declare no widget.",
        db_default=[],
    )
    catalogs = models.JSONField(
        default=list,
        help_text="The spec's versioned catalogs the UI app implements whole, as name@version ('ui@1', 'scene@1'), as it registered them.",
        db_default=[],
    )
    registered_by = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="registered_modules",
        help_text="The client (UI app) that last registered this module's components and operations.",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="unique_module_name_per_organization")]

    @property
    def is_registered(self) -> bool:
        """A module only created by ``get_or_create`` (no components, no operations) validates nothing."""
        return bool(self.components) or bool(self.operations)

    def get_components(self) -> list[CatalogComponentInputModel]:
        return [CatalogComponentInputModel(**component) for component in self.components]

    def get_operations(self) -> list[CatalogOperationInputModel]:
        return [CatalogOperationInputModel(**operation) for operation in self.operations]

    def get_widget_defaults(self) -> list[WidgetDefaultInputModel]:
        return [WidgetDefaultInputModel(**default) for default in self.widget_defaults]


class Toolbox(models.Model):
    name = models.CharField(max_length=1000)
    description = models.TextField()
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="toolboxes",
        help_text="The user that created this Shortcut",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="toolboxes",
        help_text="The client this Toolbox belongs to",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="toolboxes",
        help_text="The Organization this Collection belongs to",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


class Shortcut(models.Model):
    name = models.CharField(max_length=1000)
    description = models.TextField(null=True, blank=True)
    toolbox = models.ForeignKey(Toolbox, on_delete=models.CASCADE, related_name="shortcuts")
    action = models.ForeignKey("Action", on_delete=models.CASCADE, related_name="shortcuts", null=True)
    implementation = models.ForeignKey("Implementation", on_delete=models.CASCADE, related_name="shortcuts", null=True)
    saved_args = models.JSONField(default=dict, db_default={})
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="shortcuts",
        help_text="The user that created this Shortcut",
    )
    bind_number = models.IntegerField(
        default=0,
        null=True,
        blank=True,
        help_text="Which shortcut should be bound to this Action by default. 0 means no binding",
        db_default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    args = models.JSONField(default=list, help_text="Inputs for this Shortcut", db_default=[])
    returns = models.JSONField(default=list, help_text="Outputs for this Shortcut", db_default=[])
    allow_quick = models.BooleanField(
        default=False,
        help_text="Allow quick execution of this Shortcut (e.g. run without confirmation)",
        db_default=False,
    )
    use_returns = models.BooleanField(
        default=False,
        help_text="Use the result of this Shortcut (e.g. use the result in the next Shortcut)",
        db_default=False,
    )


class ShortcutBinding(models.Model):
    """An admin's binding of a shortcut to a user: pinned for them, and they cannot unpin it.

    ``app`` is the identifier of the app it is pinned in (the caller's client release); null pins
    it in every app. A binding only pins: the shortcut stays visible to the whole organization.
    """

    shortcut = models.ForeignKey(Shortcut, on_delete=models.CASCADE, related_name="bindings")
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, related_name="shortcut_bindings", help_text="The user this shortcut is pinned for")
    app = models.CharField(max_length=2000, null=True, blank=True, help_text="The identifier of the app it is pinned in; null for every app")
    bound_by = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, related_name="bound_shortcuts", help_text="The admin who bound it")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            # One every-app binding too: null counts as a value here.
            models.UniqueConstraint(fields=["shortcut", "user", "app"], name="one_binding_per_shortcut_user_app", nulls_distinct=False),
        ]
