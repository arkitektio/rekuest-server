from facade.enums import DataPointType
import graphene
from graphene.types.generic import GenericScalar
from facade.types import DataModel, DataPoint



class ProviderProtocol(graphene.Enum):
    WEBSOCKET = "websocket"


class PointSettings(graphene.ObjectType):
    type = graphene.String(description="The Type of the Datapoitn")


class ProviderSettings(graphene.ObjectType):
    type = ProviderProtocol(description="The communication protocol")
    kwargs = GenericScalar(description="kwargs for the provider")

class HostProtocol(graphene.Enum):
    WEBSOCKET = "websocket"



class WardTypes(graphene.Enum):
    GRAPHQL = "graphql"
    REST = "rest"

class HostSettings(graphene.ObjectType):
    type = HostProtocol(description="The communication protocol")
    kwargs = GenericScalar(description="kwargs for the provider")

class WardSettings(graphene.ObjectType):
    type = WardTypes(description="The communication protocol")
    needsNegotiation = graphene.Boolean()
    host = graphene.String()
    port = graphene.Int()
    distinct = graphene.String()

class PostmanProtocol(graphene.Enum):
    WEBSOCKET = "websocket"
    KAFKA = "kafka"
    RABBITMQ = "rabbitmq"


class PostmanSettings(graphene.ObjectType):
    type = PostmanProtocol(description="The communication protocol")
    kwargs = GenericScalar(description="kwargs for your postman")


class Transcript(graphene.ObjectType):
    wards =  graphene.List(WardSettings, description="Connection parameters for the wards")
    extensions = GenericScalar(description="Space for extensions")
    point = graphene.Field(PointSettings)
    postman = graphene.Field(PostmanSettings)
    host = graphene.Field(HostSettings)
    provider = graphene.Field(ProviderSettings)
    timestamp = graphene.DateTime()
    models = graphene.List(DataModel, description="Registered Models in this instance")