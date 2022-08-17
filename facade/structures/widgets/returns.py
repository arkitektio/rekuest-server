from balder.registry import register_type
import graphene

get_widget_types = lambda: {
    "ImageReturnWidget": ImageReturnWidget,
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

    class Meta:
        interfaces = (ReturnWidget,)
