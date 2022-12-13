from django_filters.filters import OrderingFilter, TimeRangeFilter

import django_filters
from balder.filters import EnumFilter, MultiEnumFilter
from facade.enums import ProvisionStatus

from facade.inputs import (
    AgentStatusInput,
    AssignationStatusInput,
    LogLevelInput,
    NodeKindInput,
    ProvisionStatusInput,
)
from .models import Agent, Node, Repository, Template, Registry
from django.db.models import Q
from django.db.models import Count

# class PodFilter(django_filters.FilterSet):
#    agent = django_filters.ModelChoiceFilter(queryset=Agent.objects.all(),field_name= "template__provider")
#    status = EnumFilter(choices=PodStatus.choices, field_name="status")


class UserFilter(django_filters.FilterSet):
    username = django_filters.CharFilter(
        field_name="username",
        lookup_expr="icontains",
        label="Search for substring of name",
    )
    email = django_filters.CharFilter(
        field_name="email",
        lookup_expr="icontains",
        label="Search for substring of name",
    )


class AgentFilter(django_filters.FilterSet):
    app = django_filters.CharFilter(method="app_filter")
    registry = django_filters.ModelChoiceFilter(
        queryset=Registry.objects.all(), field_name="registry"
    )
    status = MultiEnumFilter(type=AgentStatusInput, field_name="status")
    search = django_filters.CharFilter(method="search_filter", label="Search")


    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(registry__app__name__icontains=value) | Q(template__interface__icontains=value)
        )


class NodeFilter(django_filters.FilterSet):
    repository = django_filters.ModelChoiceFilter(
        queryset=Repository.objects.all(), field_name="repository"
    )
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    search = django_filters.CharFilter(method="search_filter", label="Search")
    type = EnumFilter(type=NodeKindInput, field_name="type")
    arg_types = django_filters.BaseInFilter(method="arg_types_filter", label="Args")
    interfaces = django_filters.BaseInFilter(method="interfaces_filter", label="Args")
    restrict = django_filters.BaseInFilter(method="restrict_filter", label="Restrict")
    templated = django_filters.BooleanFilter(method="templated_filter", label="Currently Templated")

    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value)
        )

    def interfaces_filter(self, queryset, name, value):
        for i in value:
            queryset = queryset.filter(interfaces__contains=i)
        return queryset

    def restrict_filter(self, queryset, name, value):
        return queryset.filter(templates__registry__unique__in=value)

    def templated_filter(self, queryset, name, value):
        return queryset.annotate(num_templates=Count('templates')).filter(num_templates__gt= 0)

    def arg_types_filter(self, queryset, name, value):
        filter_args = {}
        for index, arg in enumerate(value):
            if arg in ["IntArgPort", "StringArgPort", "dict", "float"]:
                filter_args[f"args__{index}__type"] = arg
            filter_args[f"args__{index}__identifier"] = arg

        return queryset.filter(**filter_args)


class ProvisionFilter(django_filters.FilterSet):
    status = MultiEnumFilter(type=ProvisionStatusInput, field_name="status")
    agent = django_filters.ModelChoiceFilter(queryset=Agent.objects, field_name="agent")


class AssignationFilter(django_filters.FilterSet):
    status = MultiEnumFilter(type=AssignationStatusInput, field_name="status")


class ProvisionLogFilter(django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class AssignationLogFilter(django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class ReservationLogFilter(django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class NodesFilter(django_filters.FilterSet):
    package = django_filters.CharFilter(field_name="package", lookup_expr="icontains")


class RegistryFilter(django_filters.FilterSet):
    unique = django_filters.CharFilter(field_name="unique", lookup_expr="icontains")


class TemplateFilter(django_filters.FilterSet):
    package = django_filters.CharFilter(
        field_name="node__package", lookup_expr="icontains"
    )
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    interface = django_filters.CharFilter(
        field_name="node__interface", lookup_expr="icontains"
    )
    providable = django_filters.BooleanFilter(
        method="providable_filter", label="Get active pods?"
    )
    node = django_filters.ModelChoiceFilter(queryset=Node.objects, field_name="node")

    def providable_filter(self, queryset, name, value):
        return queryset.filter(agent__active=True)

    class Meta:
        model = Template
        fields = ("node",)
