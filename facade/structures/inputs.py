import graphene
from facade.enums import AgentStatus, AssignationStatus, LogLevel, NodeType, ProvisionStatus, ReservationStatus
from balder.enum import InputEnum
from facade.structures.ports.input import ArgPortInput, KwargPortInput, ReturnPortInput


NodeTypeInput = InputEnum.from_choices(NodeType)

ReservationStatusInput = InputEnum.from_choices(ReservationStatus)
AgentStatusInput = InputEnum.from_choices(AgentStatus)
ProvisionStatusInput = InputEnum.from_choices(ProvisionStatus)
AssignationStatusInput = InputEnum.from_choices(AssignationStatus)

LogLevelInput = InputEnum.from_choices(LogLevel)

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
    )
    interface = graphene.String(description="The Interface", required=True)
    package = graphene.String(description="The Package", required=False)
