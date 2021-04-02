import graphene
from balder.registry import register_type


get_port_types = lambda: {
            "IntReturnPort": IntReturnPort,
            "ModelReturnPort": ModelReturnPort,
            "StringReturnPort": StringReturnPort,
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
        _type = instance.get("type")
        return typemap.get(_type, ReturnPort)

@register_type
class IntReturnPort(graphene.ObjectType):
    """Int Port"""
    class Meta:
        interfaces = (ReturnPort,)

@register_type
class StringReturnPort(graphene.ObjectType):
    """String Port"""
    class Meta:
        interfaces = (ReturnPort,)

@register_type
class ModelReturnPort(graphene.ObjectType):
    """Model Port"""
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (ReturnPort,)


    