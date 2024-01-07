from balder.registry import register_type
import graphene
from facade.structures.widgets.types import Choice
return_widget_types = {
    "ImageReturnWidget": lambda: ImageReturnWidget,
    "CustomReturnWidget": lambda: CustomReturnWidget,
    "ChoiceReturnWidget": lambda: ChoiceReturnWidget,
}


@register_type
class ReturnWidget(graphene.Interface):
    kind = graphene.String(required=True)

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = return_widget_types
        _type = instance.get("kind")
        return typemap.get(_type, lambda: ReturnWidget)()


@register_type
class ImageReturnWidget(graphene.ObjectType):
    query = graphene.String(description="A query that returns an image path")
    ward = graphene.String(description="A hook for the app to call")

    class Meta:
        interfaces = (ReturnWidget,)


@register_type
class CustomReturnWidget(graphene.ObjectType):
    hook = graphene.String(description="A hook for the app to call")
    ward = graphene.String(description="A hook for the app to call")

    class Meta:
        interfaces = (ReturnWidget,)


@register_type
class ChoiceReturnWidget(graphene.ObjectType):
    choices = graphene.List(Choice, description="A list of choices")

    class Meta:
        interfaces = (ReturnWidget,)
