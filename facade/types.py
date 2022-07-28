from typing import Type
from facade.scalars import Any
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
from facade import models
from lok.models import LokApp as HerreAppModel
from balder.types import BalderObject
import graphene
from balder.registry import register_type
from graphene.types.generic import GenericScalar

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
        description="The desired amount of Instances", required=False
    )
    minimalInstances = graphene.Int(
        description="The minimal amount of Instances", required=False
    )


class ReserveParams(graphene.ObjectType):
    registries = graphene.List(
        graphene.ID, description="Registry thar are allowed", required=False
    )
    agents = graphene.List(
        graphene.ID, description="Agents that are allowed", required=False
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


class PortType(graphene.Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    ENUM = "ENUM"
    DICT = "DICT"


class ChildPort(graphene.ObjectType):
    type = PortType(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    identifier = graphene.String(description="The corresponding Model")
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)


class ArgPort(graphene.ObjectType):
    key = graphene.String(required=True)
    label = graphene.String()
    type = PortType(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    identifier = graphene.String(description="The corresponding Model")
    nullable = graphene.Boolean()
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)
    widget = graphene.Field(Widget, description="Description of the Widget")


class KwargPort(graphene.ObjectType):
    key = graphene.String(required=True)
    type = PortType(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    label = graphene.String()
    identifier = graphene.String(description="The corresponding Model")
    nullable = graphene.Boolean()
    default = Any()
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)
    widget = graphene.Field(Widget, description="Description of the Widget")


class ReturnPort(graphene.ObjectType):
    key = graphene.String(required=True)
    type = PortType(description="the type of input", required=False)
    label = graphene.String()
    description = graphene.String(
        description="A description for this Port", required=False
    )
    identifier = graphene.String(description="The corresponding Model")
    nullable = graphene.Boolean()
    child = graphene.Field(lambda: ChildPort, description="The child", required=False)
    widget = graphene.Field(ReturnWidget, description="A return widget")


class LokApp(BalderObject):
    class Meta:
        model = HerreAppModel


class LokUser(BalderObject):
    class Meta:
        model = get_user_model()


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
    class Meta:
        model = models.Agent


class Waiter(BalderObject):
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

    class Meta:
        model = models.Assignation


class ProvisionLog(BalderObject):
    class Meta:
        model = models.ProvisionLog


class ProvisionParams(graphene.ObjectType):
    autoUnprovide = graphene.Boolean(required=False)


class Provision(BalderObject):
    params = graphene.Field(ProvisionParams)
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
    args = graphene.List(ArgPort)
    kwargs = graphene.List(KwargPort)
    returns = graphene.List(ReturnPort)
    interfaces = graphene.List(graphene.String)
    templates = BalderFiltered(
        Template, filterset_class=TemplateFilter, related_field="templates"
    )
    repository = BalderInheritedField(lambda: Repository, related_field="repository")

    class Meta:
        model = models.Node


@register_type
class Repository(BalderInheritedModel):

    id = graphene.ID(description="Id of the Repository")
    nodes = BalderFiltered(Node, filterset_class=NodesFilter, related_field="nodes")
    name = graphene.String(
        description="The Name of the Repository",
        deprecation_reason="Will be replaced in the future",
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


class Reservation(BalderObject):
    params = graphene.Field(ReserveParams)
    log = BalderFiltered(
        ReservationLog, filterset_class=ReservationLogFilter, related_field="log"
    )

    class Meta:
        model = models.Reservation


class Permission(BalderObject):
    unique = graphene.String(description="Unique ID for this permission", required=True)

    def resolve_unique(root, info):
        return f"{root.content_type.app_label}.{root.codename}"

    class Meta:
        model = PermissionModel


class Group(BalderObject):
    class Meta:
        model = GroupModel


class User(BalderObject):
    color = graphene.String(description="The associated color for this user")

    def resolve_color(root, info):
        if hasattr(root, "meta"):
            return root.meta.color
        return "#FF0000"

    class Meta:
        model = get_user_model()
        description = get_user_model().__doc__
