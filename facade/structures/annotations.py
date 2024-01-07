from balder.registry import register_type
import graphene
from graphene.types.generic import GenericScalar


annotation_types = {
    "ValueRange": lambda: ValueRange,
    "CustomAnnotation": lambda: CustomAnnotation,
    "IsPredicate": lambda : IsPredicate,
    "AttributePredicate": lambda : AttributePredicate,
}





class Annotation(graphene.Interface):
    kind = graphene.String(description="The name of the annotation")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = annotation_types
        _type = instance.get("kind")
        return typemap.get(_type, lambda: Annotation)()


@register_type
class ValueRange(graphene.ObjectType):
    min = graphene.Float(description="The minimum value", required=True)
    max = graphene.Float(description="The maximum value", required=True)

    class Meta:
        interfaces = (Annotation,)
@register_type
class CustomAnnotation(graphene.ObjectType):
    args = graphene.List(graphene.String, description="The arguments for this annotation")
    hook = graphene.String(description="The hook for this annotation", required=True)

    class Meta:
        interfaces = (Annotation,)


class IsPredicateType(graphene.Enum):
    LOWER = "LOWER"
    HIGHER = "HIGHER"
    DIGIT = "DIGIT"

    

@register_type
class IsPredicate(graphene.ObjectType):
    predicate = graphene.Field(IsPredicateType, description="The arguments for this annotation", required=True)

    class Meta:
        interfaces = (Annotation,)


@register_type
class AttributePredicate(graphene.ObjectType):
    """ A predicate that checks if an atribute fullfills a certain condition """
    attribute = graphene.String(description="The attribute to check", required=True)
    annotations = graphene.List(Annotation, description="The annotations for this attribute")

    class Meta:
        interfaces = (Annotation,)