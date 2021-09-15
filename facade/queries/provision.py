from facade.enums import ProvisionStatus, ProvisionStatusInput
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

    class Arguments:
        exclude = graphene.List(ProvisionStatusInput, description="The excluded values", required=False)
        filter = graphene.List(ProvisionStatusInput, description="The included values", required=False)

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None):
        qs = Provision.objects.filter(creator=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Provision
        list = True