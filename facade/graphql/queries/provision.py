from balder.types import BalderQuery
from facade import types
from facade.models import Provision, Reservation, Agent, Registry
import graphene
from facade.filters import ProvisionFilter
from lok import bounced
from guardian.shortcuts import get_objects_for_user
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
        filter = ProvisionFilter
        list = True
        operation = "allprovisions"


class LinkableProvisions(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query provisions", required=True)

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        x = Reservation.objects.get(id=id)
        prov_queryset = get_objects_for_user(
            info.context.user,
            "facade.can_link_to",
        )

        return prov_queryset.filter(template__in=x.node.templates.all())

    class Meta:
        type = types.Provision
        list = True
        operation = "linkableprovisions"


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
        operation = "myprovisions"


class Provisions(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            ProvisionStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ProvisionStatusInput, description="The included values", required=False
        )
        identifier = graphene.List(
            graphene.String,
            description="The agent identifier",
            required=False,
            default_value="default",
        )

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None, identifier="default"):

        creator = info.context.bounced.user
        client = info.context.bounced.client

       
        registry, _ = Registry.objects.update_or_create(user=creator, client=client, defaults=dict(app=info.context.bounced.app))
        agent, _ = Agent.objects.get_or_create(registry=registry, identifier=identifier)

        qs = Provision.objects.filter(agent=agent)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Provision
        list = True
        paginate = True
        operation = "provisions"
