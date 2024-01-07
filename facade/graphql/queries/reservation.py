from balder.types import BalderQuery
from facade import types, models
from facade.models import Reservation, Node
import graphene
from graphene.types.generic import GenericScalar
from lok import bounced

from facade.inputs import ReservationStatusInput, TemplateParamInput, PortDemandInput


class ReservationDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query reservation", required=False)
        provision = graphene.ID(description="The parent provision", required=False)
        reference = graphene.String(description="The reference", required=False)

    @bounced(anonymous=True)
    def resolve(root, info, id=None, provision: str = None, reference: str = None):
        if id:
            return Reservation.objects.get(id=id)
        elif provision and reference:
            return Reservation.objects.get(provision=provision, reference=reference)
        else:
            raise Exception("No id or provision and reference provided")

    class Meta:
        type = types.Reservation
        operation = "reservation"


class AllReservations(BalderQuery):
    class Meta:
        type = types.Reservation
        list = True
        operation = "allreservations"


class MyReservations(BalderQuery):
    class Arguments:
        exclude = graphene.List(
            ReservationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ReservationStatusInput, description="The included values", required=False
        )

    @bounced(anonymous=False)
    def resolve(root, info, exclude=None, filter=None):
        qs = Reservation.objects.filter(waiter__registry__user=info.context.user)
        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)

        return qs.all()

    class Meta:
        type = types.Reservation
        list = True
        paginate = True
        operation = "myreservations"


class ReservationsQuery(BalderQuery):
    class Arguments:
        instance_id = graphene.String(required=True)
        exclude = graphene.List(
            ReservationStatusInput, description="The excluded values", required=False
        )
        filter = graphene.List(
            ReservationStatusInput, description="The included values", required=False
        )
        input_port_demands = graphene.List(PortDemandInput, required=False)
        output_port_demands = graphene.List(PortDemandInput, required=False)
        node_interfaces = graphene.List(graphene.String, required=False)
        template_params = graphene.List(TemplateParamInput, required=False)

    @bounced(only_jwt=True)
    def resolve(
        root,
        info,
        exclude=None,
        filter=None,
        instance_id=None,
        node_interfaces=None,
        template_params=None,
        input_port_demands=None,
        output_port_demands=None,
    ):
        creator = info.context.bounced.user
        client = info.context.bounced.client

        registry, _ = models.Registry.objects.update_or_create(
            user=creator, client=client, defaults=dict(app=info.context.bounced.app)
        )
        waiter, _ = models.Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

        qs = Reservation.objects.filter(waiter=waiter)

        if template_params:
            for param in template_params:
                if param.value:
                    qs = qs.filter(
                        **{f"provisions__template__params__{param.key}": param.value}
                    )

        if filter:
            qs = qs.filter(status__in=filter)
        if exclude:
            qs = qs.exclude(status__in=exclude)
        if node_interfaces:
            for i in node_interfaces:
                qs = qs.filter(node__interfaces__contains=i)

        if input_port_demands:
            nodes = Node.objects.matching_demands(input_demands=input_port_demands)
            print(nodes)
            qs = qs.filter(node__in=nodes)
 
        return qs

    class Meta:
        type = types.Reservation
        list = True
        operation = "reservations"
