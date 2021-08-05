from django.db.models.aggregates import Count
from facade.enums import NodeType, PodStatus, ProvisionStatus
import django_filters
from .models import Node,Provider, Template
from balder.fields.enum import EnumFilter
from django.db.models import Q

class PodFilter(django_filters.FilterSet):
    provider = django_filters.ModelChoiceFilter(queryset=Provider.objects.all(),field_name= "template__provider")
    status = EnumFilter(choices=PodStatus.choices)

class ProviderFilter(django_filters.FilterSet):
    app = django_filters.CharFilter(method="app_filter")

    def app_filter(self, queryset, name, value):
        return queryset.filter(app__client_id=value)

    class Meta:
        model = Provider
        fields = ("active",)

class NodeFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    search = django_filters.CharFilter(method="search_filter",label="Search")
    type = EnumFilter(choices=NodeType.choices)
    arg_types = django_filters.BaseInFilter(method="arg_types_filter", label="Args")

    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value))

    def arg_types_filter(self, queryset, name, value):
        filter_args = {}
        for index, arg in enumerate(value):
            if arg in ["IntArgPort","StringArgPort","dict","float"]:
                filter_args[f"args__{index}__type"] = arg
            filter_args[f"args__{index}__identifier"] = arg

        return queryset.filter(**filter_args)


class ProvisionFilter(django_filters.FilterSet):
    active = django_filters.BooleanFilter(method="active_filter", label="Get active Provisions")

    def active_filter(self, queryset, name, value):
        return queryset.filter(status__in=[ProvisionStatus.ACTIVE])


class NodesFilter(django_filters.FilterSet):
    active = django_filters.BooleanFilter(method="active_filter", label="Get active Provisions")

    def active_filter(self, queryset, name, value):
        return queryset.filter(status__in=[ProvisionStatus.ACTIVE])


class TemplateFilter(django_filters.FilterSet):
    package = django_filters.CharFilter(field_name="node__package", lookup_expr="icontains")
    interface = django_filters.CharFilter(field_name="node__interface", lookup_expr="icontains")
    provided = django_filters.BooleanFilter(method="provided_filter", label="Get active pods?")
    providable = django_filters.BooleanFilter(method="providable_filter", label="Get active pods?")
    node = django_filters.ModelChoiceFilter(queryset=Node.objects,field_name= "node")

    def provided_filter(self, queryset, name, value):
        print(queryset.all())
        return queryset.filter(pods__status=PodStatus.ACTIVE)

    def providable_filter(self, queryset, name, value):
        return queryset.filter(provider__active=True)

    class Meta:
        model = Template
        fields = ("node",)