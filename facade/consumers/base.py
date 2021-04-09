

import aiormq
from delt.messages.exception import ExceptionMessage
from delt.messages.base import MessageModel
from channels.generic.websocket import AsyncWebsocketConsumer
from delt.messages.utils import expandToMessage, MessageError
import json
import logging

logger = logging.getLogger(__name__)

class BaseConsumer(AsyncWebsocketConsumer):
    mapper = None

    def __init__(self, *args, **kwargs):
        self.channel = None # The connection layer will be async set by the provider
        assert self.mapper is not None; "Cannot instatiate this Consumer without a Mapper"
        super().__init__(*args, **kwargs)

    async def catch(self, text_data, exception=None):
        raise NotImplementedError(f"Received untyped request {text_data}: {exception}")

    async def send_message(self, message: MessageModel):
        await self.send(text_data=message.to_channels())


    async def forward(self, message: MessageModel, routing_key):
        """Forwards the message to our provessing layer

        Args:
            message (MessageModel): [description]
            routing_key ([type]): The Routing Key (Topic or somethign)
        """

        await self.channel.basic_publish(
                message.to_message(), routing_key=routing_key,
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=message.meta.reference
        )
        )


    async def receive(self, text_data):
        try:
            json_dict = json.loads(text_data)
            try:
                message = expandToMessage(json_dict)
                function = self.mapper[message.__class__](self)
                await function(message)

            except MessageError as e:
                logger.error(f"{self.__class__.__name__} e")
                await self.send_message(ExceptionMessage.fromException(e, json_dict["meta"]["reference"]))
                raise e

        except Exception as e:
            logger.error(e)
            self.catch(text_data)
            raise e

