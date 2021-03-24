import graphene
from graphene.types.generic import GenericScalar
from facade.structures.widgets.input import WidgetInput

class ArgPortInput(graphene.InputObjectType):
    key=  graphene.String(description="The Key", required=True)
    type = graphene.String(description="the type of input", required=True)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this inport")
    identifier= graphene.String(description="The corresponding Model")
    widget = graphene.Field(WidgetInput, description="Which Widget to use to render Port in User Interfaces")


class KwargPortInput(graphene.InputObjectType):
    key=  graphene.String(description="The Key", required=True)
    type = graphene.String(description="the type of input", required=True)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this inport")
    default = GenericScalar(description="Does this field have a specific value")
    identifier= graphene.String(description="The corresponding Model")
    widget = graphene.Field(WidgetInput, description="Which Widget to use to render Port in User Interfaces")

class ReturnPortInput(graphene.InputObjectType):
    key =  graphene.String(description="The Key", required=True)
    type = graphene.String(description="the type of input", required=True)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this Outport")
    identifier= graphene.String(description="The corresponding Model")