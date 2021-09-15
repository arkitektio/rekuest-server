import graphene
from graphene.types.generic import GenericScalar

class WidgetInput(graphene.InputObjectType):
    typename = graphene.String(description="type", required=True)
    query = graphene.String(description="Do we have a possible")
    dependencies = graphene.List(graphene.String, description="The dependencies of this port")
    max = graphene.String(description="Max value for int widget")
    min = graphene.String(description="Max value for int widget")