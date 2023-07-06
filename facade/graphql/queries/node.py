from facade.filters import NodeFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Node, Template
import graphene
from lok import bounced

from facade.scalars import QString


class NodeDetailQuery(BalderQuery):
    """Asss

    Is A query for all of these specials in the world
    """

    class Arguments:
        q = graphene.Argument(QString, description="The identifier string")
        id = graphene.ID(description="The query node")
        hash = graphene.String(description="The query node")
        template = graphene.ID(
            description="Get node for a template (overrides the others)"
        )
    
    def resolve(root, info, template=None, **kwargs):
        if template:
            return Template.objects.get(id=template).node
        return Node.objects.get(**kwargs)

    class Meta:
        type = types.Node
        operation = "node"


class Nodes(BalderQuery):
    class Meta:
        type = types.Node
        list = True
        paginate = True
        filter = NodeFilter
        operation = "allnodes"
