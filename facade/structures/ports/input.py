import graphene
from graphene.types.generic import GenericScalar
from facade.structures.widgets.input import WidgetInput


class ArgPortInput(graphene.InputObjectType):
    key = graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    identifier = graphene.String(description="The corresponding Model")
    widget = graphene.Field(
        WidgetInput, description="Which Widget to use to render Port in User Interfaces"
    )
    label = graphene.String(description="The corresponding label")
    child = graphene.Field(lambda: ArgPortInput, description="The Child of this")
    options = GenericScalar(description="Options for an Enum")


class KwargPortInput(graphene.InputObjectType):
    key = graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    label = graphene.String(description="The corresponding label")

    defaultDict = GenericScalar(description="Does this field have a specific value")
    defaultOption = GenericScalar(description="Does this field have a specific value")
    defaultInt = graphene.Int(description="Does this field have a specific value")
    defaultBool = graphene.Boolean(description="Does this field have a specific value")
    defaultFloat = graphene.Float(description="Does this field have a specific value")
    defaultID = graphene.ID(description="Does this field have a specific value")
    defaultString = graphene.String(description="Does this field have a specific value")
    defaultList = graphene.List(
        GenericScalar, description="Does this field have a specific value"
    )
    identifier = graphene.String(description="The corresponding Model")
    widget = graphene.Field(
        WidgetInput, description="Which Widget to use to render Port in User Interfaces"
    )
    child = graphene.Field(lambda: KwargPortInput, description="The Child of this")
    options = GenericScalar(description="Options for an Enum")


class ReturnPortInput(graphene.InputObjectType):
    key = graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(
        description="A description for this Port", required=False
    )
    label = graphene.String(description="The corresponding label")
    identifier = graphene.String(description="The corresponding Model")
    child = graphene.Field(lambda: ReturnPortInput, description="The Child of this")
