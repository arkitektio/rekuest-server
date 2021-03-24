from facade.structures.widgets.types import Widget
from balder.registry import register_type
import graphene

get_port_types = lambda: {
            "int": IntArgPort,
            "model": StringArgPort,
            "string": ModelArgPort
}

@register_type
class ArgPort(graphene.Interface):
    key = graphene.String()
    type = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)
    required = graphene.Boolean()
    widget = graphene.Field(Widget,description="Description of the Widget")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.get("type")
        return typemap.get(_type, ArgPort)

@register_type
class IntArgPort(graphene.ObjectType):
    """Integer Port"""
    default = graphene.Int(description="Default value")

    class Meta:
        interfaces = (ArgPort,)


@register_type
class StringArgPort(graphene.ObjectType):
    """String Port"""
    default = graphene.String(description="Default value")

    class Meta:
        interfaces = (ArgPort,)





@register_type
class ModelArgPort(graphene.ObjectType):
    """Model Port"""
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (ArgPort,)

    