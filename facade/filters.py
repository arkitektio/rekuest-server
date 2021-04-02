from facade.enums import NodeType, PodStatus
import django_filters
from .models import Pod, BaseProvider
from balder.fields.enum import EnumFilter
from django.db.models import Q

class PodFilter(django_filters.FilterSet):
    provider = django_filters.ModelChoiceFilter(queryset=BaseProvider.objects.all(),field_name= "template__provider")
    status = EnumFilter(choices=PodStatus.choices)

class ProviderFilter(django_filters.FilterSet):

    class Meta:
        model = BaseProvider
        fields = ("active",)

class NodeFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    search = django_filters.CharFilter(method="search_filter",label="Search")
    type = EnumFilter(choices=NodeType.choices)

    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value))


class TemplateFilter(django_filters.FilterSet):
    package = django_filters.CharFilter(field_name="node__package", lookup_expr="icontains")
    interface = django_filters.CharFilter(field_name="node__interface", lookup_expr="icontains")
    active = django_filters.BooleanFilter(method="active_filter", label="Get active pods?")

    def active_filter(self, queryset, name, value):
        return queryset.filter(pods__status=PodStatus.ACTIVE)