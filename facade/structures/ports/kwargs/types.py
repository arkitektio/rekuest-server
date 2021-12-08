from graphene.types.generic import GenericScalar
from facade.structures.widgets.types import Widget
from balder.registry import register_type
import graphene

get_port_types = lambda: {
    "IntKwargPort": IntKwargPort,
    "StringKwargPort": StringKwargPort,
    "StructureKwargPort": StructureKwargPort,
    "ListKwargPort": ListKwargPort,
    "BoolKwargPort": BoolKwargPort,
    "EnumKwargPort": EnumKwargPort,
    "DictKwargPort": DictKwargPort,
}


@register_type
class KwargPort(graphene.Interface):
    key = graphene.String()
    type = graphene.String()
    label = graphene.String()
    description = graphene.String(required=False)
    required = graphene.Boolean()
    widget = graphene.Field(Widget, description="Description of the Widget")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_port_types()
        _type = instance.get("type", instance.get("typename"))
        return typemap.get(_type, KwargPort)


@register_type
class IntKwargPort(graphene.ObjectType):
    """Integer Port"""

    defaultInt = graphene.Int(description="Default value")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class BoolKwargPort(graphene.ObjectType):
    """Integer Port"""

    defaultBool = graphene.Boolean(description="Default value")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class EnumKwargPort(graphene.ObjectType):
    """Integer Port"""

    defaultOption = GenericScalar(description="The Default Value")
    options = GenericScalar(description="A dict of options")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class StringKwargPort(graphene.ObjectType):
    """String Port"""

    defaultString = graphene.String(description="Default value")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class StructureKwargPort(graphene.ObjectType):
    """Model Port"""

    defaultID = graphene.ID(description="The Id")
    identifier = graphene.String(description="The identifier of this Model")
    bound = graphene.String(description="Where is this Model Boudn")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class ListKwargPort(graphene.ObjectType):
    """Model Port"""

    defaultList = graphene.List(GenericScalar, description="TheList")
    child = graphene.Field(lambda: KwargPort, description="The child")

    class Meta:
        interfaces = (KwargPort,)


@register_type
class DictKwargPort(graphene.ObjectType):
    """Model Port"""

    defaultDict = GenericScalar(description="TheList")
    child = graphene.Field(lambda: KwargPort, description="The child")

    class Meta:
        interfaces = (KwargPort,)
