import graphene
from graphene.types.generic import GenericScalar
from facade.structures.widgets.input import WidgetInput

class InPortInput(graphene.InputObjectType):
    key=  graphene.String(description="The Key", required=True)
    type = graphene.String(description="the type of input", required=True)
    description = graphene.String(description="A description for this Port", required= False)
    required= graphene.Boolean(description="Is this field required", required=True)
    primary = graphene.Boolean(description="Is this a primary port", required=False)
    label = graphene.String(description="The Label of this inport")
    default = GenericScalar(description="Does this field have a specific value")
    identifier= graphene.String(description="The corresponding Model")
    widget = graphene.Field(WidgetInput, description="Which Widget to use to render Port in User Interfaces")


class OutPortInput(graphene.InputObjectType):
    key =  graphene.String(description="The Key", required=True)
    type = graphene.String(description="the type of input", required=True)
    description = graphene.String(description="A description for this Port", required= False)
    label = graphene.String(description="The Label of this Outport")
    identifier= graphene.String(description="The corresponding Model")