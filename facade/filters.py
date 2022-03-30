from django_filters.filters import OrderingFilter, TimeRangeFilter

import django_filters
from balder.filters import EnumFilter, MultiEnumFilter
from facade.enums import ProvisionStatus

from facade.structures.inputs import AgentStatusInput, AssignationStatusInput, LogLevelInput, NodeTypeInput, ProvisionStatusInput
from .models import Node, Repository, Template
from django.db.models import Q

# class PodFilter(django_filters.FilterSet):
#    agent = django_filters.ModelChoiceFilter(queryset=Agent.objects.all(),field_name= "template__provider")
#    status = EnumFilter(choices=PodStatus.choices, field_name="status")


class AgentFilter(django_filters.FilterSet):
    app = django_filters.CharFilter(method="app_filter")
    status = MultiEnumFilter(type=AgentStatusInput, field_name="status")

    def app_filter(self, queryset, name, value):
        return queryset.filter(registry__app__client_id=value)


class NodeFilter(django_filters.FilterSet):
    repository = django_filters.ModelChoiceFilter(
        queryset=Repository.objects.all(), field_name="repository"
    )
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    search = django_filters.CharFilter(method="search_filter", label="Search")
    type = EnumFilter(type=NodeTypeInput, field_name="type")
    arg_types = django_filters.BaseInFilter(method="arg_types_filter", label="Args")

    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value)
        )

    def arg_types_filter(self, queryset, name, value):
        filter_args = {}
        for index, arg in enumerate(value):
            if arg in ["IntArgPort", "StringArgPort", "dict", "float"]:
                filter_args[f"args__{index}__type"] = arg
            filter_args[f"args__{index}__identifier"] = arg

        return queryset.filter(**filter_args)


class ProvisionFilter(django_filters.FilterSet):
    status = MultiEnumFilter(type=ProvisionStatusInput, field_name="status")


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
    package = django_filters.CharFilter(
        field_name="package", lookup_expr="icontains"
    )



class TemplateFilter(django_filters.FilterSet):
    package = django_filters.CharFilter(
        field_name="node__package", lookup_expr="icontains"
    )
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
