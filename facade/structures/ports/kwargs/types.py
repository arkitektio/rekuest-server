from facade.structures.widgets.types import Widget
from balder.registry import register_type
import graphene

get_port_types = lambda: {
            "IntKwargPort": IntKwargPort,
            "StringKwargPort": StringKwargPort,
            "StructureKwargPort": StructureKwargPort,
            "ListKwargPort": ListKwargPort,
}

@register_type
class KwargPort(graphene.Interface):
    key = graphene.String()
    type = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)
    required = graphene.Boolean()
    widget = graphene.Field(Widget,description="Description of the Widget")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.get("type", instance.get("typename"))
        return typemap.get(_type, KwargPort)

@register_type
class IntKwargPort(graphene.ObjectType):
    """Integer Port"""
    default = graphene.Int(description="Default value")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class StringKwargPort(graphene.ObjectType):
    """String Port"""
    default = graphene.String(description="Default value")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class StructureKwargPort(graphene.ObjectType):
    """Model Port"""
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (KwargPort,)

@register_type
class ListKwargPort(graphene.ObjectType):
    """Model Port"""
    child = graphene.Field(lambda: KwargPort, description="The child")

    class Meta:
        interfaces = (KwargPort,)


    