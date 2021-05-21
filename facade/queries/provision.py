from facade.enums import ProvisionStatus
from facade.filters import NodeFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.models import Provision, ReservationStatus
import graphene
from herre import bounced


class ProvisionDetailQuery(BalderQuery):

    class Arguments:
        reference = graphene.ID(description="The query provisions", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, reference=None):
        return Provision.objects.get(reference=reference)

    class Meta:
        type = types.Provision
        operation = "provision"



class Provisions(BalderQuery):


    class Meta:
        type = types.Provision
        list = True


class MyProvisions(BalderQuery):

    @bounced(anonymous=False)
    def resolve(root, info, **kwargs):
        return Provision.objects.filter(creator=info.context.user).exclude(status__in=[ProvisionStatus.ENDED.value,ProvisionStatus.CANCELLED.value]).all()


    class Meta:
        type = types.Provision
        list = True