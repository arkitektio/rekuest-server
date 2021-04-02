from facade.filters import NodeFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.models import Node, Service
import graphene
from herre import bounced


class ServiceDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The serive node")

    @bounced(anonymous=True)
    def resolve(root, info, **kwargs):
        return Service.objects.get(**kwargs)

    class Meta:
        type = types.Service
        operation = "service"



class Services(BalderQuery):

    @bounced(anonymous=True)
    def resolve(root, info, **kwargs):
        return Service.objects.all()

    class Meta:
        type = types.Service
        list = True