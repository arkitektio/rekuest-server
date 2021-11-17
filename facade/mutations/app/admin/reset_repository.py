from balder.types.mutation.base import BalderMutation
from lok import bounced
import graphene

from facade.models import AppRepository, Node


class ResetRepositoryReturn(graphene.ObjectType):
    ok = graphene.Boolean()


class ResetRepository(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        pass

    class Meta:
        type = ResetRepositoryReturn

    @bounced(anonymous=False)
    def mutate(root, info, url=None, name=None):
        repository = AppRepository.objects.get(app=info.context.bounced.app)

        for node in Node.objects.filter(repository=repository).all():
            node.delete()

        return {"ok": True}
