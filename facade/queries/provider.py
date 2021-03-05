from facade.filters import PodFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.enums import PodStatus
from facade.models import Provider
import graphene
from herre import bounced
from balder.enum import InputEnum

class ProviderDetailQuery(BalderQuery):

    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Provider.objects.get(id=id)

    class Meta:
        type = types.Provider
        operation = "provider"



class Providers(BalderQuery):

    class Meta:
        type = types.Provider
        list = True