from facade.helpers import create_context_from_bounced
from facade.models import Assignation
from facade.workers.gateway import GatewayConsumer
import uuid
from delt.messages import BouncedUnassignMessage
from facade import types
from balder.types import BalderMutation
from graphene.types.generic import GenericScalar
from lok import bounced
import graphene
import logging

logger = logging.getLogger(__name__)  #


class UnassignMutation(BalderMutation):
    class Arguments:
        assignation = graphene.ID(
            description="The reference of the Assignation you want to ruin"
        )
        reference = graphene.String(
            description="An identifier you want this unassignation to go buy",
            required=False,
        )

    class Meta:
        type = types.Assignation
        operation = "unassign"

    @bounced(only_jwt=True)
    def mutate(root, info, assignation=None):
        reference = str(uuid.uuid4())
        bounce = info.context.bounced

        assignation = Assignation.objects.get(reference=assignation)

        bounced = BouncedUnassignMessage(
            data={
                "assignation": assignation.reference,
                "provision": assignation.provision.reference,
            },
            meta={
                "reference": reference,
                "extensions": {
                    "callback": "not-set",
                    "progress": "not-set",
                    "persist": True,
                },
                "context": create_context_from_bounced(bounce),
            },
        )

        GatewayConsumer.send(bounced)

        return assignation
