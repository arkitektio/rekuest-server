import uuid
from facade import types
from facade.models import Provision
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class ProvideMutation(BalderMutation):
    """Scan allows you to add Datapoints to your Arnheim Schema, this is only available to Admin users"""

    class Arguments:
        node = graphene.ID(required=False)
        template = graphene.ID(required=False)
        reference = graphene.String(required=False)
        params = GenericScalar(description="Additional Params")

    class Meta:
        type = types.Provision
        operation = "provide"

    @bounced(only_jwt=True)
    def mutate(root, info, node=None, template=None, params={}, reference=None):
        reference = reference or str(uuid.uuid4())
        bounce = info.context.bounced

        pro = Provision.objects.create(
            **{
                "node_id": node,
                "template_id": template,
                "params": params,
                "reference": reference,
                "creator": bounce.user,
                "app": bounce.app,
            }
        )

        return pro
