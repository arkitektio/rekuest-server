from facade.filters import RegistryFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Registry
import graphene
from lok import bounced


class RegistryDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Registry.objects.get(id=id)

    class Meta:
        type = types.Registry
        operation = "registry"


class Registries(BalderQuery):
    class Meta:
        type = types.Registry
        list = True
        filter = RegistryFilter
