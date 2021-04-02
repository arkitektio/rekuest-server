from balder.registry import register_type
import graphene

get_widget_types = lambda: {
            "QueryWidget": QueryWidget,
            "IntWidget": IntWidget
}

@register_type
class Widget(graphene.Interface):
    type = graphene.String()
    dependencies = graphene.List(graphene.String, description="The set-keys this widget depends on, check *query parameters*")

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_widget_types()
        _type = instance.get("type")
        return typemap.get(_type, Widget)


@register_type
class QueryWidget(graphene.ObjectType):
    query = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


    
@register_type
class IntWidget(graphene.ObjectType):
    query = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)