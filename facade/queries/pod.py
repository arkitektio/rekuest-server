from facade.filters import PodFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.enums import PodStatus
from facade.models import Node, Pod
import graphene
from herre import bounced
from balder.enum import InputEnum

class PodDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Pod.objects.get(id=id)

    class Meta:
        type = types.Pod
        operation = "pod"



class Pods(BalderQuery):

    class Arguments:
        status = graphene.Argument(InputEnum.from_choices(PodStatus), description="The choice of the pods")
        provider = graphene.String(description="The Name of the provider")


    @bounced(anonymous=True)
    def resolve(root, info, status = None, provider=None):
        qs = Pod.objects
        qs = qs.filter(status=status) if status else qs
        qs = qs.filter(template__provider__name=provider) if provider else qs
        return qs.all()


    class Meta:
        type = types.Pod
        list = True