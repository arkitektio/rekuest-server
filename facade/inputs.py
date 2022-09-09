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

from facade.scalars import Any, AnyInput


NodeKindInput = InputEnum.from_choices(NodeKind)

ReservationStatusInput = InputEnum.from_choices(ReservationStatus)
AgentStatusInput = InputEnum.from_choices(AgentStatus)
ProvisionStatusInput = InputEnum.from_choices(ProvisionStatus)
AssignationStatusInput = InputEnum.from_choices(AssignationStatus)

LogLevelInput = InputEnum.from_choices(LogLevel)


class PortKindInput(graphene.Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    DICT = "DICT"
    FLOAT = "FLOAT"


class ChoiceInput(graphene.InputObjectType):
    value = AnyInput(required=True)
    label = graphene.String(required=True)


class WidgetInput(graphene.InputObjectType):
    kind = graphene.String(description="type", required=True)
    query = graphene.String(description="Do we have a possible")
    dependencies = graphene.List(
        graphene.String, description="The dependencies of this port"
    )
    choices = graphene.List(ChoiceInput, description="The dependencies of this port")
    max = graphene.Int(description="Max value for int widget")
    min = graphene.Int(description="Max value for int widget")
    placeholder = graphene.String(description="Placeholder for any widget")
    as_paragraph = graphene.Boolean(description="Is this a paragraph")
    hook = graphene.String(description="A hook for the app to call")


class ReturnWidgetInput(graphene.InputObjectType):
    kind = graphene.String(description="type", required=True)
    query = graphene.String(description="Do we have a possible")
    hook = graphene.String(description="A hook for the app to call")


class ChildPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    name = graphene.String(description="The name of this port")
    kind = PortKindInput(description="The type of this port")
    description = graphene.String(description="The description of this port")
    child = graphene.Field(lambda: ChildPortInput, description="The child port")
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)


class ArgPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    name = graphene.String(description="The name of this argument")
    label = graphene.String(description="The name of this argument")
    kind = PortKindInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    widget = graphene.Field(WidgetInput, description="The child of this argument")
    default = Any(description="The key of the arg", required=False)
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)


class ReturnPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    name = graphene.String(description="The name of this argument")
    label = graphene.String(description="The name of this argument")
    kind = PortKindInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    widget = graphene.Field(ReturnWidgetInput, description="The child of this argument")
    nullable = graphene.Boolean(description="Is this argument nullable", required=True)


class DefinitionInput(graphene.InputObjectType):
    """A definition for a node"""

    description = graphene.String(
        description="A description for the Node", required=False
    )
    name = graphene.String(description="The name of this template", required=True)
    args = graphene.List(ArgPortInput, description="The Args")
    returns = graphene.List(ReturnPortInput, description="The Returns")
    interfaces = graphene.List(
        graphene.String,
        description="The Interfaces this node provides makes sense of the metadata",
    )  # todo infer interfaces from args kwargs
    kind = graphene.Argument(
        NodeKindInput,
        description="The variety",
        default_value=NodeKind.FUNCTION.value,
        required=True,
    )
    interface = graphene.String(description="The Interface", required=True)
    package = graphene.String(description="The Package", required=False)
    meta = GenericScalar(description="The metadata", required=False)
