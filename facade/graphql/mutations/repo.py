from facade import models
from facade import types
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)


class CreateMirrorReturn(graphene.ObjectType):
    created = graphene.Boolean()
    repo = graphene.Field(types.MirrorRepository)


class CreateMirror(BalderMutation):
    """Create Repostiory"""

    class Arguments:
        name = graphene.String(description="The name of this template", required=True)
        url = graphene.String(description="A Url for the Mirror", required=True)

    class Meta:
        type = CreateMirrorReturn

    @bounced(anonymous=True)
    def mutate(root, info, url=None, name=None):

        repo, created = models.MirrorRepository.objects.update_or_create(
            url=url, name=name
        )
        return {"created": created, "repo": repo}


class DeleteRepoReturn(graphene.ObjectType):
    id = graphene.String()


class DeleteRepo(BalderMutation):
    """Create an experiment (only signed in users)"""

    class Arguments:
        id = graphene.ID(description="The Id of the Mirror", required=True)

    @bounced()
    def mutate(root, info, id, **kwargs):
        repo = models.Repository.objects.get(id=id)
        repo.delete()
        return {"id": id}

    class Meta:
        type = DeleteRepoReturn
        operation = "deleterepo"


class UpdateMirrorReturn(graphene.ObjectType):
    id = graphene.String()


class UpdateMirror(BalderMutation):
    """Create an experiment (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="A cleartext description what this representation represents as data",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        repo = models.MirrorRepository.objects.get(id=id)
        repo.scan()

        return {"id": id}

    class Meta:
        type = UpdateMirrorReturn
