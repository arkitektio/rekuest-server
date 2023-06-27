from typing import Type
from facade.scalars import Any, Identifier
from graphene.types.interface import InterfaceOptions
from django.contrib.auth import get_user_model
from facade.filters import (
    AssignationFilter,
    AssignationLogFilter,
    NodesFilter,
    ProvisionLogFilter,
    ReservationLogFilter,
    TemplateFilter,
    ProvisionFilter,
)
from balder.fields.filtered import BalderFiltered
from facade.structures.widgets.types import Widget
from facade.structures.widgets.returns import ReturnWidget
from facade.global_enums import LogicalCondition, EffectKind
from facade import models
from facade.inputs import Scope, NodeScope
from lok.models import LokApp as HerreAppModel, LokClient as LokClientModel
from balder.types import BalderObject
import graphene
from balder.registry import register_type
from graphene.types.generic import GenericScalar
from facade.structures.annotations import Annotation
from django.contrib.auth.models import (
    Permission as PermissionModel,
    Group as GroupModel,
)


class BalderInheritedModelOptions(InterfaceOptions):
    child_models = {}


class BalderInheritedModel(graphene.Interface):
    @classmethod
    def __init_subclass_with_meta__(cls, _meta=None, child_models=None, **options):
        if not _meta:
            _meta = BalderInheritedModelOptions(cls)
        if child_models:
            _meta.child_models = child_models

        super(BalderInheritedModel, cls).__init_subclass_with_meta__(
            _meta=_meta, **options
        )

    @classmethod
    def resolve_inherited(cls, instance, info):
        for key, value in cls._meta.child_models.items():
            attr_name = key.__name__.lower()
            if hasattr(instance, attr_name):
                return getattr(instance, attr_name)

    @classmethod
    def resolve_type(cls, instance, info):
        for key, value in cls._meta.child_models.items():
            if isinstance(instance, key):
                return value()


class BalderInheritedField(graphene.Field):
    def __init__(
        self,
        _type: Type[BalderInheritedModel],
        resolver=None,
        related_field=None,
        **kwargs,
    ):
        resolver = lambda root, info: self.type.resolve_inherited(
            getattr(root, related_field), info
        )
        super().__init__(_type, resolver=resolver, **kwargs)


class ReserveParamsInput(graphene.InputObjectType):
    autoProvide = graphene.Boolean(
        description="Do you want to autoprovide", required=False
    )
    autoUnprovide = graphene.Boolean(
        description="Do you want to auto_unprovide", required=False
    )
    registries = graphene.List(
        graphene.ID, description="Registry thar are allowed", required=False
    )
    agents = graphene.List(
        graphene.ID, description="Agents that are allowed", required=False
    )
    templates = graphene.List(
        graphene.ID, description="Templates that can be selected", required=False
    )
    desiredInstances = graphene.Int(
        description="The desired amount of Instances", required=True, default=1
    )
    minimalInstances = graphene.Int(
        description="The minimal amount of Instances", required=True, default=1
    )


class ReserveParams(graphene.ObjectType):
    registries = graphene.List(
        graphene.ID, description="Registry thar are allowed", required=False
    )
    templates = graphene.List(
        graphene.ID, description="Templates that can be selected", required=False
    )
    desiredInstances = graphene.Int(description="The desired amount of Instances")
    autoProvide = graphene.Boolean(description="Autoproviding")
    autoUnprovide = graphene.Boolean(description="Autounproviding")
    minimalInstances = graphene.Int(
        description="The minimal amount of Instances", required=False
    )


class PortKind(graphene.Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    DICT = "DICT"
    FLOAT = "FLOAT"


class ChildPort(graphene.ObjectType):
    kind = PortKind(description="the type of input", required=True)
    identifier = Identifier(description="The corresponding Model")
    scope = Scope(description="The scope of this port", required=True)
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)
    default = Any()
    annotations = graphene.List(Annotation, description="The annotations of this port")
    assign_widget = graphene.Field(Widget, description="Description of the Widget")
    return_widget = graphene.Field(ReturnWidget, description="A return widget")


class PortGroup(graphene.ObjectType):
    key = graphene.String(required=True)
    hidden = graphene.Boolean(required=False)


class Dependency(graphene.ObjectType):
    key = graphene.String(
        required=False, description="The key of the port (null should be self)"
    )
    condition = LogicalCondition(
        required=True, description="The condition of the dependency"
    )
    value = GenericScalar(required=True)


class Effect(graphene.ObjectType):
    dependencies = graphene.List(
        Dependency, description="The dependencies of this effect"
    )
    kind = EffectKind(required=True, description="The condition of the dependency")
    message = graphene.String()


class Port(graphene.ObjectType):
    key = graphene.String(required=True)
    label = graphene.String()
    kind = PortKind(description="the type of input", required=True)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    effects = graphene.List(Effect, description="The effects of this port")
    identifier = Identifier(description="The corresponding Model")
    scope = Scope(description="The scope of this port", required=True)
    nullable = graphene.Boolean(required=True)
    default = Any()
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)
    annotations = graphene.List(Annotation, description="The annotations of this port")
    assign_widget = graphene.Field(Widget, description="Description of the Widget")
    return_widget = graphene.Field(ReturnWidget, description="A return widget")
    groups = graphene.List(
        graphene.String, description="The port groups", required=False
    )

    class Meta:
        description = "A Port"


class LokApp(BalderObject):
    class Meta:
        model = HerreAppModel


class LokClient(BalderObject):
    class Meta:
        model = LokClientModel


class Registry(BalderObject):
    name = graphene.String(
        deprecation_reason="Will be replaced in the future",
    )

    class Meta:
        model = models.Registry


class Structure(BalderObject):
    repository = BalderInheritedField(lambda: Repository, related_field="repository")

    class Meta:
        model = models.Structure


class Scan(graphene.ObjectType):
    ok = graphene.Boolean()


class DataQuery(graphene.ObjectType):
    structures = graphene.List(Structure, description="The queried models on the")


class Agent(BalderObject):
    client_id = graphene.String(required=True)

    def resolve_client_id(self, info):
        return self.registry.client.client_id

    class Meta:
        model = models.Agent


class Waiter(BalderObject):
    client_id = graphene.String(required=True)

    def resolve_client_id(self, info):
        return self.registry.client.client_id

    class Meta:
        model = models.Waiter


class ReservationLog(BalderObject):
    class Meta:
        model = models.ReservationLog


class AssignationLog(BalderObject):
    class Meta:
        model = models.AssignationLog


class Assignation(BalderObject):
    log = BalderFiltered(
        AssignationLog, filterset_class=AssignationLogFilter, related_field="log"
    )
    args = graphene.List(Any)
    returns = graphene.List(Any)

    class Meta:
        model = models.Assignation


class ProvisionLog(BalderObject):
    class Meta:
        model = models.ProvisionLog


class ProvisionParams(graphene.ObjectType):
    autoUnprovide = graphene.Boolean(required=False)


class Provision(BalderObject):
    params = graphene.Field(ProvisionParams)
    template = graphene.Field(lambda: Template, required=True)
    log = BalderFiltered(
        ProvisionLog, filterset_class=ProvisionLogFilter, related_field="log"
    )
    assignations = BalderFiltered(
        Assignation, filterset_class=AssignationFilter, related_field="assignations"
    )

    class Meta:
        model = models.Provision


class TemplateParams(graphene.ObjectType):
    maximumInstances = graphene.Int()


class Template(BalderObject):
    extensions = graphene.List(
        graphene.String, description="The extentions of this template"
    )
    provisions = BalderFiltered(
        Provision, filterset_class=ProvisionFilter, related_field="provisions"
    )
    params = graphene.Field(GenericScalar)

    class Meta:
        model = models.Template


class Node(BalderObject):
    args = graphene.List(Port)
    returns = graphene.List(Port)
    interfaces = graphene.List(graphene.String)
    templates = BalderFiltered(
        Template, filterset_class=TemplateFilter, related_field="templates"
    )
    scope = NodeScope(description="The scope of this port", required=True)
    port_groups = graphene.List(
        PortGroup, description="The port groups", required=False
    )
    is_test_for = BalderFiltered(
        lambda: Node,
        model=models.Node,
        filterset_class=NodesFilter,
        description="The nodes this node tests",
        related_field="is_test_for",
    )
    tests = BalderFiltered(
        lambda: Node,
        model=models.Node,
        filterset_class=NodesFilter,
        description="The tests of its node",
        related_field="tests",
    )

    class Meta:
        model = models.Node


@register_type
class Repository(BalderInheritedModel):
    id = graphene.ID(description="Id of the Repository", required=True)
    nodes = BalderFiltered(Node, filterset_class=NodesFilter, related_field="nodes")
    name = graphene.String(
        description="The Name of the Repository",
    )

    class Meta:
        child_models = {
            models.AppRepository: lambda: AppRepository,
            models.MirrorRepository: lambda: MirrorRepository,
        }


@register_type
class AppRepository(BalderObject):
    class Meta:
        model = models.AppRepository
        interfaces = (Repository,)


@register_type
class MirrorRepository(BalderObject):
    class Meta:
        model = models.MirrorRepository
        interfaces = (Repository,)


class Binds(graphene.ObjectType):
    clients = graphene.List(LokClient, description="The clients of this bind")
    templates = graphene.List(Template, description="The templates of this bind")

    def resolve_clients(self, info):
        return LokClientModel.objects.filter(client_id__in=self["clients"])

    def resolve_templates(self, info):
        return models.Template.objects.filter(id__in=self["templates"])


class Reservation(BalderObject):
    params = graphene.Field(ReserveParams)
    binds = graphene.Field(Binds)
    log = BalderFiltered(
        ReservationLog, filterset_class=ReservationLogFilter, related_field="log"
    )

    class Meta:
        model = models.Reservation


class Collection(BalderObject):
    nodes = BalderFiltered(
        lambda: Node,
        model=models.Node,
        filterset_class=NodesFilter,
        description="The nodes this collection has",
        related_field="nodes",
    )

    class Meta:
        model = models.Collection


class TestCase(BalderObject):
    class Meta:
        model = models.TestCase


class TestResult(BalderObject):
    class Meta:
        model = models.TestResult
