import graphene
from graphene.types.generic import GenericScalar
from facade.structures.widgets.input import WidgetInput

class ArgPortInput(graphene.InputObjectType):
    key=  graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this inport")
    identifier= graphene.String(description="The corresponding Model")
    widget = graphene.Field(WidgetInput, description="Which Widget to use to render Port in User Interfaces")
    child = graphene.Field(lambda: ArgPortInput, description="The Child of this")
    transpile= graphene.String(description="The corresponding Model")
    options = GenericScalar(description="Options for an Enum")


class KwargPortInput(graphene.InputObjectType):
    key=  graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this inport")
    default = GenericScalar(description="Does this field have a specific value")
    identifier= graphene.String(description="The corresponding Model")
    widget = graphene.Field(WidgetInput, description="Which Widget to use to render Port in User Interfaces")
    child = graphene.Field(lambda: KwargPortInput, description="The Child of this")
    transpile= graphene.String(description="The corresponding Model")
    options = GenericScalar(description="Options for an Enum")

class ReturnPortInput(graphene.InputObjectType):
    key =  graphene.String(description="The Key", required=False)
    type = graphene.String(description="the type of input", required=False)
    typename = graphene.String(description="the type of input", required=False)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this Outport")
    identifier= graphene.String(description="The corresponding Model")
    child = graphene.Field(lambda: ReturnPortInput, description="The Child of this")
    transpile= graphene.String(description="The corresponding Model")