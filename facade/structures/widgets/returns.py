from balder.registry import register_type
import graphene

get_widget_types = lambda: {
    "ImageReturnWidget": ImageReturnWidget,
    "CustomReturnWidget": CustomReturnWidget,
}


@register_type
class ReturnWidget(graphene.Interface):
    kind = graphene.String(required=True)

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_widget_types()
        _type = instance.get("kind")
        return typemap.get(_type, ReturnWidget)


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
