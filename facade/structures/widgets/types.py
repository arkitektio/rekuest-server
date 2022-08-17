from balder.registry import register_type
import graphene
from graphene.types.generic import GenericScalar

get_widget_types = lambda: {
    "QueryWidget": QueryWidget,
    "IntWidget": IntWidget,
    "StringWidget": StringWidget,
    "SearchWidget": SearchWidget,
    "SliderWidget": SliderWidget,
    "LinkWidget": LinkWidget,
    "BoolWidget": BoolWidget,
    "ChoiceWidget": ChoiceWidget,
}


@register_type
class Widget(graphene.Interface):
    kind = graphene.String(required=True)
    dependencies = graphene.List(
        graphene.String,
        description="The set-keys this widget depends on, check *query parameters*",
    )

    @classmethod
    def resolve_type(cls, instance, info):
        typemap = get_widget_types()
        _type = instance.get("kind")
        return typemap.get(_type, Widget)


@register_type
class QueryWidget(graphene.ObjectType):
    query = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


@register_type
class LinkWidget(graphene.ObjectType):
    linkbuilder = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


@register_type
class SearchWidget(graphene.ObjectType):
    query = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


@register_type
class BoolWidget(graphene.ObjectType):
    class Meta:
        interfaces = (Widget,)


class Choice(graphene.ObjectType):
    value = GenericScalar(required=True)
    label = graphene.String(required=True)


@register_type
class ChoiceWidget(graphene.ObjectType):
    choices = graphene.List(Choice, description="A list of choices")

    class Meta:
        interfaces = (Widget,)


@register_type
class IntWidget(graphene.ObjectType):
    query = graphene.String(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


@register_type
class SliderWidget(graphene.ObjectType):
    min = graphene.Int(description="A Complex description")
    max = graphene.Int(description="A Complex description")

    class Meta:
        interfaces = (Widget,)


@register_type
class StringWidget(graphene.ObjectType):
    placeholder = graphene.String(description="A placeholder to display")

    class Meta:
        interfaces = (Widget,)
