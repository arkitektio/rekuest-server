import graphene
from graphene.types.generic import GenericScalar
from facade.types import DataModel, DataPoint



class PostmanProtocol(graphene.Enum):
    WEBSOCKET = "websocket"
    KAFKA = "kafka"
    RABBITMQ = "rabbitmq"


class PostmanSettings(graphene.ObjectType):
    type = PostmanProtocol(description="The communication protocol")
    kwargs = GenericScalar(description="kwargs for your postman")


class Transcript(graphene.ObjectType):
    extensions = GenericScalar(description="Space for extensions")
    postman = graphene.Field(PostmanSettings)
    models = graphene.List(DataModel)
    timestamp = graphene.DateTime()
    models = graphene.List(DataModel, description="Registered Models in this instance")