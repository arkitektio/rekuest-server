from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.models import Node, Pod
import graphene
from herre import bounced


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

    class Meta:
        type = types.Pod
        list = True