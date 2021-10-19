import graphene
from balder.registry import register_type
from graphene.types.generic import GenericScalar


get_port_types = lambda: {
            "IntReturnPort": IntReturnPort,
            "StringReturnPort": StringReturnPort,
            "StructureReturnPort": StructureReturnPort,
            "ListReturnPort": ListReturnPort,
            "BoolReturnPort": BoolReturnPort,
            "EnumReturnPort": EnumReturnPort
}

@register_type
class ReturnPort(graphene.Interface):
    type = graphene.String()
    key = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.get("type", instance.get("typename"))
        return typemap.get(_type, ReturnPort)

@register_type
class IntReturnPort(graphene.ObjectType):
    """Int Port"""
    class Meta:
        interfaces = (ReturnPort,)

@register_type
class BoolReturnPort(graphene.ObjectType):
    """Int Port"""
    class Meta:
        interfaces = (ReturnPort,)

@register_type
class EnumReturnPort(graphene.ObjectType):
    """Integer Port"""
    options = GenericScalar(description="A dict of options")

    class Meta:
        interfaces = (ReturnPort,)



@register_type
class StringReturnPort(graphene.ObjectType):
    """String Port"""
    class Meta:
        interfaces = (ReturnPort,)


@register_type
class StructureReturnPort(graphene.ObjectType):
    """Model Port"""
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (ReturnPort,)



@register_type
class ListReturnPort(graphene.ObjectType):
    """Model Port"""
    child = graphene.Field(lambda: ReturnPort, description="The child")

    class Meta:
        interfaces = (ReturnPort,)


    