from facade.enums import PodStatus
import django_filters
from .models import Pod, Provider
from balder.fields.enum import EnumFilter
from django.db.models import Q

class PodFilter(django_filters.FilterSet):
    provider = django_filters.ModelChoiceFilter(queryset=Provider.objects.all(),field_name= "template__provider")
    status = EnumFilter(choices=PodStatus.choices)


class NodeFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="search_filter",label="Search")

    def search_filter(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value))