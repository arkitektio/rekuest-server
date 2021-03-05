import graphene
from balder.registry import register_type


get_port_types = lambda: {
            "int": IntOutPort,
            "model": ModelOutPort,
            "string": StringOutPort
}

@register_type
class OutPort(graphene.Interface):
    
    key = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.pop("type")
        return typemap.get(_type, OutPort)

@register_type
class IntOutPort(graphene.ObjectType):
    """Int Port"""
    class Meta:
        interfaces = (OutPort,)

@register_type
class StringOutPort(graphene.ObjectType):
    """String Port"""
    class Meta:
        interfaces = (OutPort,)

@register_type
class ModelOutPort(graphene.ObjectType):
    """Model Port"""
    identifier = graphene.String(description="The identifier of this Model")

    class Meta:
        interfaces = (OutPort,)


    