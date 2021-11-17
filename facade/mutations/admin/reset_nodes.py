from balder.types.mutation.base import BalderMutation
from facade.models import Node
from lok import bounced
import graphene


class ResetNodesReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetNodes(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        exclude = graphene.List(
            graphene.ID, description="Respositroys you want to exclude"
        )
        pass

    class Meta:
        type = ResetNodesReturn

    @bounced(anonymous=True)
    def mutate(root, info, exclude=[], name=None):

        for node in Node.objects.all():
            node.delete()

        return {"ok": True}
