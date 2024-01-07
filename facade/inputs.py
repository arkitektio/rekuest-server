from facade.enums import (
    AgentStatus,
    AssignationStatus,
    LogLevel,
    NodeKind,
    ProvisionStatus,
    ReservationStatus,
)
from balder.enum import InputEnum
from graphene.types.generic import GenericScalar
import graphene
from .global_enums import LogicalCondition, EffectKind
from .enums import AnnotationKind, ReturnWidgetKind, WidgetKind
from facade.scalars import Any, AnyInput, SearchQuery, Identifier
from facade.structures.annotations import IsPredicateType

NodeKindInput = InputEnum.from_choices(NodeKind)

ReservationStatusInput = InputEnum.from_choices(ReservationStatus)
AgentStatusInput = InputEnum.from_choices(AgentStatus)
ProvisionStatusInput = InputEnum.from_choices(ProvisionStatus)
AssignationStatusInput = InputEnum.from_choices(AssignationStatus)

LogLevelInput = InputEnum.from_choices(LogLevel)


class TemplateParamInput(graphene.InputObjectType):
    key = graphene.String(required=True)
    value = GenericScalar(required=False)


class PortKindInput(graphene.Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    DICT = "DICT"
    FLOAT = "FLOAT"
    UNION = "UNION"
    DATE = "DATE"


class PortDemandInput(graphene.InputObjectType):
    at = graphene.Int(required=False)
    key = graphene.String(required=False)  # Needs specific key
    kind = PortKindInput(required=False)  # if false == Any
    identifier = graphene.String(required=False)  # if false == Any
    nullable = graphene.Boolean(required=False)  # if false == Any
    variants = graphene.List(lambda: PortDemandInput, required=False)  # if false == Any
    child = graphene.Field(lambda: PortDemandInput, required=False)  # if false == Any


class ChoiceInput(graphene.InputObjectType):
    value = AnyInput(required=True)
    label = graphene.String(required=True)
    description = graphene.String(required=False)


class MessageKind(graphene.Enum):
    TERMINATE = "TERMINATE"  # terminate the assignation
    CANCEL = "CANCEL"  # Cancel an ongoing assignation (e.g. )
    ASSIGN = "ASSIGN"  # Assign, execute, and return (only available for definitions that are function or generators)

    TELL = "TELL"  # Tell and forget


class Scope(graphene.Enum):
    GLOBAL = "GLOBAL"
    LOCAL = "LOCAL"


class NodeScope(graphene.Enum):
    GLOBAL = "GLOBAL"
    LOCAL = "LOCAL"
    BRIDGE_GLOBAL_TO_LOCAL = "BRIDGE_GLOBAL_TO_LOCAL"
    BRIDGE_LOCAL_TO_GLOBAL = "BRIDGE_LOCAL_TO_GLOBAL"


class MessageInput(graphene.InputObjectType):
    kind = MessageKind(required=True)
    text = graphene.String(required=True)
    reference = graphene.String(required=True)
    data = AnyInput(required=True)


class TemplateFieldInput(graphene.InputObjectType):
    parent = graphene.String(required=False, description="The parent key (if nested)")
    key = graphene.String(required=True, description="The key of the field")
    type = graphene.String(required=True, description="The key of the field")
    description = graphene.String(
        required=False, description="A short description of the field"
    )


class DependencyInput(graphene.InputObjectType):
    key = graphene.String(
        required=False, description="The key of the port, defaults to self"
    )
    condition = graphene.Argument(
        LogicalCondition, required=True, description="The condition of the dependency"
    )
    value = AnyInput(required=True)


class EffectInput(graphene.InputObjectType):
    dependencies = graphene.List(
        DependencyInput, description="The dependencies of this effect"
    )
    kind = graphene.Argument(
        EffectKind, required=True, description="The condition of the dependency"
    )
    message = graphene.String()


class WidgetInput(graphene.InputObjectType):
    kind = graphene.Argument(WidgetKind, description="type", required=True)
    query = SearchQuery(description="Do we have a possible")

    choices = graphene.List(ChoiceInput, description="The dependencies of this port")
    max = graphene.Float(description="Max value for slider widget")
    min = graphene.Float(description="Min value for slider widget")
    step = graphene.Float(description="Step value for slider widget")
    placeholder = graphene.String(description="Placeholder for any widget")
    as_paragraph = graphene.Boolean(description="Is this a paragraph")
    hook = graphene.String(description="A hook for the app to call")
    ward = graphene.String(description="A ward for the app to call")
    fields = graphene.List(
        TemplateFieldInput,
        description="The fields of this widget (onbly on TemplateWidget)",
        required=False,
    )


class ReturnWidgetInput(graphene.InputObjectType):
    kind = graphene.Argument(ReturnWidgetKind, description="type", required=True)
    choices = graphene.List(ChoiceInput, description="The dependencies of this port")
    query = graphene.String(description="Do we have a possible")
    hook = graphene.String(description="A hook for the app to call")
    ward = graphene.String(description="A hook for the app to call")


class ChildPortInput(graphene.InputObjectType):
    identifier = Identifier(description="The identifier")
    scope = graphene.Argument(
        Scope, description="The scope of this port", required=True
    )
    name = graphene.String(description="The name of this port")
    kind = PortKindInput(description="The type of this port")
    child = graphene.Field(lambda: ChildPortInput, description="The child port")
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)
    annotations = graphene.List(
        lambda: AnnotationInput, description="The annotations of this argument"
    )
    variants = graphene.List(
        lambda: ChildPortInput,
        description="The varients of this port (only for union)",
        required=False,
    )
    assign_widget = graphene.Field(
        WidgetInput, description="The child of this argument"
    )
    return_widget = graphene.Field(
        ReturnWidgetInput, description="The child of this argument"
    )


class AnnotationInput(graphene.InputObjectType):
    kind = graphene.Argument(
        AnnotationKind, description="The kind of annotation", required=True
    )
    name = graphene.String(description="The name of this annotation")
    args = graphene.String(description="The value of this annotation")
    min = graphene.Float(description="The min of this annotation (Value Range)")
    max = graphene.Float(description="The max of this annotation (Value Range)")
    hook = graphene.String(description="A hook for the app to call")
    predicate = graphene.Argument(
        IsPredicateType, description="The predicate of this annotation (IsPredicate)"
    )
    attribute = graphene.String(description="The attribute to check")
    annotations = graphene.List(
        lambda: AnnotationInput, description="The annotation of this annotation"
    )


class PortInput(graphene.InputObjectType):
    effects = graphene.List(EffectInput, description="The dependencies of this port")
    identifier = Identifier(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    scope = graphene.Argument(
        Scope, description="The scope of this port", required=True
    )
    variants = graphene.List(
        ChildPortInput,
        description="The varients of this port (only for union)",
        required=False,
    )
    name = graphene.String(description="The name of this argument")
    label = graphene.String(description="The name of this argument")
    kind = PortKindInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    assign_widget = graphene.Field(
        WidgetInput, description="The child of this argument"
    )
    return_widget = graphene.Field(
        ReturnWidgetInput, description="The child of this argument"
    )
    default = Any(description="The key of the arg", required=False)
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)
    annotations = graphene.List(
        AnnotationInput, description="The annotations of this argument"
    )
    groups = graphene.List(
        graphene.String, description="The port group of this argument"
    )


class ReserveBindsInput(graphene.InputObjectType):
    templates = graphene.List(
        graphene.ID,
        description="The templates that we are allowed to use",
        required=True,
    )
    clients = graphene.List(
        graphene.ID, description="The clients that we are allowed to use", required=True
    )


class PortGroupInput(graphene.InputObjectType):
    key = graphene.String(description="The key of the port group", required=True)
    hidden = graphene.Boolean(description="Is this port group hidden", required=False)


class DefinitionInput(graphene.InputObjectType):
    """A definition for a template"""

    description = graphene.String(
        description="A description for the Node", required=False
    )
    collections = graphene.List(graphene.ID, required=False)
    name = graphene.String(description="The name of this template", required=True)
    port_groups = graphene.List(PortGroupInput, required=True)
    args = graphene.List(PortInput, description="The Args", required=True)
    returns = graphene.List(PortInput, description="The Returns", required=True)
    interfaces = graphene.List(
        graphene.String,
        description="The Interfaces this node provides makes sense of the metadata",
        required=True,
    )  # todo infer interfaces from args kwargs
    kind = graphene.Argument(
        NodeKindInput,
        description="The variety",
        default_value=NodeKind.FUNCTION.value,
        required=True,
    )
    is_test_for = graphene.List(
        graphene.String, description="The nodes this is a test for", required=False
    )
    pure = graphene.Boolean(required=False, default_value=False)
    idempotent = graphene.Boolean(required=False, default_value=False)
