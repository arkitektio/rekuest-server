from facade.structures.widgets.types import Widget
from balder.registry import register_type
import graphene

get_port_types = lambda: {
            "int": IntInPort,
            "model": ModelInPort,
}

@register_type
class InPort(graphene.Interface):
    key = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)
    required = graphene.Boolean()
    widget = graphene.Field(Widget,description="Description of the Widget")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.get("type")
        return typemap.get(_type, InPort)

@register_type
class IntInPort(graphene.ObjectType):
    default = graphene.Int(description="Default value")

    class Meta:
        interfaces = (InPort,)

@register_type
class ModelInPort(graphene.ObjectType):
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (InPort,)

    