from balder.types.mutation.base import BalderMutation
from delt.service.types import ServiceType
from herre.utils import decode_token
from facade.structures.transcript import HostProtocol, HostSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import AppProvider, DataModel, Service
from balder.enum import InputEnum
from facade.enums import ClientType, NodeType
from herre import bounced
import graphene
import logging
import namegenerator

logger = logging.getLogger(__name__)

class Negotiate(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        client_type = graphene.Argument(InputEnum.from_choices(ClientType),description="The type of Client")

    class Meta:
        type = Transcript

    
    @bounced(only_jwt=True)
    def mutate(root, info, client_type = ClientType.HOST):

        extensions = {}
        for service in Service.objects.filter(types__contains=ServiceType.NEGOTIATE.value).all():
            extensions[service.name] = service.negotiate(info.context.auth)

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


        if client_type == ClientType.PROVIDER.value:
            if info.context.bounced.user is not None:
                app_name = info.context.bounced.app_name + " by " + info.context.bounced.user.username
            else:
                app_name = info.context.bounced.app_name

            provider , _ = AppProvider.objects.update_or_create(client_id=info.context.bounced.client_id, user=info.context.user, defaults= {"name": app_name })
            #TODO: Check if this client can register as item
            transcript_dict["provider"] = ProviderSettings(
                type = ProviderProtocol.WEBSOCKET,
                kwargs = {
                        "host": "p-tnagerl-lab1",
                        "protocol": "ws",
                        "port": 8090,
                        "provider": provider.id,
                        "auth": {
                            "type": "token",
                            "token": info.context.auth.token
                        }
                }
            )

        if client_type == ClientType.HOST.value or client_type == ClientType.PROVIDER.value:
            transcript_dict["host"] = HostSettings(
                type = HostProtocol.WEBSOCKET,
                kwargs = {
                        "host": "p-tnagerl-lab1",
                        "protocol": "ws",
                        "port": 8090,
                        "auth": {
                            "type": "token",
                            "token": info.context.auth.token
                        }
                }
            )

        print(transcript_dict)

        return Transcript(**transcript_dict)