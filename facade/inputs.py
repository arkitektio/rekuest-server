from facade.enums import (
    AgentStatus,
    AssignationStatus,
    LogLevel,
    NodeType,
    ProvisionStatus,
    ReservationStatus,
)
from balder.enum import InputEnum
from graphene.types.generic import GenericScalar
import graphene

from facade.scalars import Any


NodeTypeInput = InputEnum.from_choices(NodeType)

ReservationStatusInput = InputEnum.from_choices(ReservationStatus)
AgentStatusInput = InputEnum.from_choices(AgentStatus)
ProvisionStatusInput = InputEnum.from_choices(ProvisionStatus)
AssignationStatusInput = InputEnum.from_choices(AssignationStatus)

LogLevelInput = InputEnum.from_choices(LogLevel)


class PortTypeInput(graphene.Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    ENUM = "ENUM"
    DICT = "DICT"


class WidgetInput(graphene.InputObjectType):
    typename = graphene.String(description="type", required=True)
    query = graphene.String(description="Do we have a possible")
    dependencies = graphene.List(
        graphene.String, description="The dependencies of this port"
    )
    max = graphene.Int(description="Max value for int widget")
    min = graphene.Int(description="Max value for int widget")
    placeholder = graphene.String(description="Placeholder for any widget")


class ChildPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    name = graphene.String(description="The name of this port")
    type = graphene.String(description="The type of this port")
    description = graphene.String(description="The description of this port")


class ArgPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    name = graphene.String(description="The name of this argument")
    label = graphene.String(description="The name of this argument")
    type = PortTypeInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    widget = graphene.Field(WidgetInput, description="The child of this argument")


class KwargPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    default = Any(description="The key of the arg", required=True)
    label = graphene.String(description="The name of this argument")
    name = graphene.String(description="The name of this argument")
    type = PortTypeInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    widget = graphene.Field(WidgetInput, description="The child of this argument")


class ReturnPortInput(graphene.InputObjectType):
    identifier = graphene.String(description="The identifier")
    key = graphene.String(description="The key of the arg", required=True)
    name = graphene.String(description="The name of this argument")
    label = graphene.String(description="The name of this argument")
    type = PortTypeInput(description="The type of this argument", required=True)
    description = graphene.String(description="The description of this argument")
    child = graphene.Field(ChildPortInput, description="The child of this argument")
    widget = graphene.Field(WidgetInput, description="The child of this argument")


class DefinitionInput(graphene.InputObjectType):
    """A definition for a node"""

    description = graphene.String(
        description="A description for the Node", required=False
    )
    name = graphene.String(description="The name of this template", required=True)
    args = graphene.List(ArgPortInput, description="The Args")
    kwargs = graphene.List(KwargPortInput, description="The Kwargs")
    returns = graphene.List(ReturnPortInput, description="The Returns")
    interfaces = graphene.List(
        graphene.String,
        description="The Interfaces this node provides [eg. bridge, filter]",
    )  # todo infer interfaces from args kwargs
    type = graphene.Argument(
        NodeTypeInput,
        description="The variety",
        default_value=NodeType.FUNCTION.value,
        required=True,
    )
    interface = graphene.String(description="The Interface", required=True)
    package = graphene.String(description="The Package", required=False)
