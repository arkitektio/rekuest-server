from balder.types.mutation.base import BalderMutation
from facade.structures.transcript import HostProtocol, HostSettings, PointSettings, PostmanProtocol, PostmanSettings, ProviderProtocol, ProviderSettings, Transcript
from facade import types
from facade.models import Provider, DataModel, DataPoint
from balder.enum import InputEnum
from facade.enums import ClientType, DataPointType, NodeType
from herre import bounced
import graphene
import logging
import namegenerator

logger = logging.getLogger(__name__)

class Negotiate(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        client_type = graphene.Argument(InputEnum.from_choices(ClientType),description="The type of Client")
        inward = graphene.String(required=False, description="Only applicable if you are a Point Provider. The adress how data requests may reach you")
        outward = graphene.String(required=False, description="Only applicable if you are a Point Provider. The adress how data requests may reach you")
        internal = graphene.Boolean(required=False, description="Are you requiring internal access to resources?")
        port = graphene.Int(required=False, description="The Port we can use to reach you")
        version = graphene.String(required=False, description="Point type")
        needs_negotiation = graphene.Boolean(required=False, default_value=False, description="If your app requires negotiation on connection!")
        point_type = graphene.Argument(InputEnum.from_choices(DataPointType), description="The points type", default_value=DataPointType.GRAPHQL.value)

    class Meta:
        type = Transcript

    
    @bounced(only_jwt=True)
    def mutate(root, info, client_type = ClientType.HOST, inward=None, outward=None, port=None, version="0.1.0", point_type= None, needs_negotiation=False, internal=False):
        if "provider" in info.context.bounced.scopes: provider, _ = Provider.objects.update_or_create(app=info.context.bounced.app, user=info.context.bounced.user, defaults= {"name": info.context.bounced.app.name })

        transcript_dict = {
            "models": DataModel.objects.all(),
            "postman": PostmanSettings(
                    type = PostmanProtocol.WEBSOCKET,
                    kwargs= {}
            )
        }
        
        transcript_dict["wards"] = [point.create_ward(internal = internal) for point in DataPoint.objects.all()]


        if client_type in [ClientType.POINT.value]:
            provider , _ = DataPoint.objects.update_or_create(app=info.context.bounced.app,
             user=info.context.bounced.user,
             defaults= 
             {
             "inward": inward,
             "outward": outward,
             "port": port,
             "type": point_type,
             "needs_negotiation": needs_negotiation
             }
             
            )
            #TODO: Check if this client can register as item
            transcript_dict["point"] = PointSettings(
                type = point_type
            )

        if client_type == ClientType.PROVIDER.value:
            transcript_dict["provider"] = ProviderSettings(
                type = ProviderProtocol.WEBSOCKET,
                kwargs = {}
            )

        if client_type == ClientType.HOST.value or client_type == ClientType.PROVIDER.value:
            transcript_dict["host"] = HostSettings(
                type = HostProtocol.WEBSOCKET,
                kwargs = {}
            )

        print(transcript_dict)

        return Transcript(**transcript_dict)