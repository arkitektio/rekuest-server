from facade.inputs import DefinitionInput
from facade import types
from facade.models import AppRepository, Node, Structure
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
import inflection

logger = logging.getLogger(__name__)



class DeleteNodeReturn(graphene.ObjectType):
    id = graphene.String()


class DeleteNode(BalderMutation):
    """Create an experiment (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="A cleartext description what this representation represents as data",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        node = Node.objects.get(id=id)
        node.delete()
        return {"id": id}

    class Meta:
        type = DeleteNodeReturn



class PurgeNodesReturn(graphene.ObjectType):
    ids = graphene.List(graphene.String)


class PurgeNodes(BalderMutation):

    class Arguments:
        app = graphene.String(required=False)

    @bounced()
    def mutate(root, info, **kwargs):
        nodes = Node.objects.filter(templates=None)
        deleting = []
        for node in nodes:
            deleting.append(node.id)
            node.delete()
        return {"ids": deleting}

    class Meta:
        type = PurgeNodesReturn

