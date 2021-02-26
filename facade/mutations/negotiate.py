from herre.utils import decode_token
from facade.structures.transcript import PostmanProtocol, PostmanSettings, Transcript
from facade import types
from facade.models import DataModel, DataPoint, Repository, Node
from balder.types import BalderMutation
from balder.enum import InputEnum
from facade.enums import ClientType, NodeType
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

def get_node_repository(user, id="localhost"):
    repo, _ = Repository.objects.filter(creator=user).get_or_create(type=f"flow", defaults={"name": f"flow_{id}", "creator": user})
    return repo

class Negotiate(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        client_type = graphene.Argument(InputEnum.from_choices(ClientType),description="The type of Client")


    class Meta:
        type = Transcript

    
    @bounced(anonymous=False)
    def mutate(root, info, client_type = ClientType.HOST):

        print(client_type)
        print(info.context.auth)


        extensions = []
        for datapoint in DataPoint.objects.all():
            extensions.append(datapoint.negotiate(info.context.auth))

        print(extensions)

        transcript_dict = {
            "extensions": extensions,
            "models": DataModel.objects.all(),
            "postman": PostmanSettings(
                    type = PostmanProtocol.WEBSOCKET,
                    kwargs= {
                        "host": "p-tnagerl-lab1",
                        "protocol": "ws",
                        "port": 8090,
                        "auth": {
                            "type": "token",
                            "token": info.context.auth.token
                        }

                    }
            )
        }



    
        return Transcript(**transcript_dict)