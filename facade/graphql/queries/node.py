from facade.filters import NodeFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Node, Template, Registry, Agent, Reservation, Assignation
import graphene
from lok import bounced
from facade.inputs import PortDemandInput
from facade.scalars import QString
from django.db import connection


class NodeDetailQuery(BalderQuery):
    """Asss

    Is A query for all of these specials in the world
    """

    class Arguments:
        q = graphene.Argument(QString, description="The identifier string")
        id = graphene.ID(description="The query node")
        hash = graphene.String(description="The query node")
        reservation = graphene.ID(
            description="The reservation that is linked to this node"
        )
        assignation = graphene.ID(
            description="The reservation that is linked to this node"
        )
        template = graphene.ID(
            description="Get node for a template (overrides the others)"
        )

    def resolve(
        root, info, template=None, reservation=None, assignation=None, **kwargs
    ):
        if reservation:
            return Reservation.objects.get(id=reservation).node
        if assignation:
            return Assignation.objects.get(id=assignation).reservation.node
        if template:
            return Template.objects.get(id=template).node
        return Node.objects.get(**kwargs)

    class Meta:
        type = types.Node
        operation = "node"


class DemandedNodes(BalderQuery):
    class Arguments:
        input_port_demands = graphene.List(PortDemandInput, required=False)
        output_port_demands = graphene.List(PortDemandInput, required=False)

    def resolve(
        root, info, input_port_demands=None, output_port_demands=None, **kwargs
    ):
        qs = Node.objects.matching_demands(input_demands=input_port_demands)

        return qs

    class Meta:
        type = types.Node
        operation = "demandednodes"
        list = True


class Nodes(BalderQuery):
    class Meta:
        type = types.Node
        list = True
        paginate = True
        filter = NodeFilter
        operation = "allnodes"
