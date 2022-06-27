from balder.types import BalderQuery
from facade import types
from facade.models import Provision
import graphene
from lok import bounced

from facade.inputs import ProvisionStatusInput


class ProvisionDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query provisions", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Provision.objects.get(id=id)

    class Meta:
        type = types.Provision
        operation = "provision"


class Provisions(BalderQuery):
    class Meta:
        type = types.Provision
        list = True


class MyProvisions(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            ProvisionStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ProvisionStatusInput, description="The included values", required=False
        )

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None):
        qs = Provision.objects.filter(agent__registry__user=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Provision
        list = True
        paginate = True
