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
    NodeScope,
)
from lok.models import LokClient
from .models import Agent, Node, Repository, Template, Registry
from django.db.models import Q
from django.db.models import Count
import graphene
from django import forms
from graphene_django.forms.converter import convert_form_field


class IDChoiceField(forms.JSONField):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def overwritten_type(self, **kwargs):
        return graphene.List(graphene.ID, **kwargs)


@convert_form_field.register(IDChoiceField)
def convert_form_field_to_string_list(field):
    return field.overwritten_type(required=field.required)


class IDChoiceFilter(django_filters.MultipleChoiceFilter):
    field_class = IDChoiceField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, field_name="pk")


class IdsFilter(django_filters.FilterSet):

    ids = IDChoiceFilter(label="Filter by values")

    def my_values_filter(self, queryset, name, value):
        if value:
            return queryset.filter(id__in=value)
        else:
            return queryset

class AgentFilter(IdsFilter, django_filters.FilterSet):
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


class NodeFilter(IdsFilter, django_filters.FilterSet):
    repository = django_filters.ModelChoiceFilter(
        queryset=Repository.objects.all(), field_name="repository"
    )
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    search = django_filters.CharFilter(method="search_filter", label="Search")
    type = EnumFilter(type=NodeKindInput, field_name="type")
    scopes = MultiEnumFilter(type=NodeScope, field_name="scope")
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


class ProvisionFilter(IdsFilter, django_filters.FilterSet):
    status = MultiEnumFilter(type=ProvisionStatusInput, field_name="status")
    agent = django_filters.ModelChoiceFilter(queryset=Agent.objects.all(), field_name="agent")
    client = django_filters.ModelChoiceFilter(queryset=LokClient.objects.all(), field_name="client")
    client_id = django_filters.CharFilter(field_name="agent__registry__client__client_id", lookup_expr="iexact")


class AssignationFilter(IdsFilter, django_filters.FilterSet):
    status = MultiEnumFilter(type=AssignationStatusInput, field_name="status")


class ProvisionLogFilter(IdsFilter, django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class AssignationLogFilter(IdsFilter, django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class ReservationLogFilter(IdsFilter, django_filters.FilterSet):
    level = EnumFilter(type=LogLevelInput, field_name="level")
    created_at = TimeRangeFilter()
    o = OrderingFilter(fields={"created_at": "time"})


class NodesFilter(IdsFilter, django_filters.FilterSet):
    package = django_filters.CharFilter(field_name="package", lookup_expr="icontains")


class RegistryFilter(IdsFilter, django_filters.FilterSet):
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
