from abc import ABC, abstractmethod

import aiormq
from delt.messages.base import MessageModel


class BaseHare(ABC):
    def __init__(self) -> None:
        self.channel = None

    @abstractmethod
    async def connect(self):
        raise NotImplementedError("Nanan")

    async def close(self):
        pass

    async def forward(self, message: MessageModel, routing_key: str):
        """Forwards the message to the layer

        Args:
            message (MessageModel): The message
            routing_key (str): The routing key
        """

        await self.channel.basic_publish(
            message.to_message(),
            routing_key=routing_key,  # Lets take the first best one
            properties=aiormq.spec.Basic.Properties(
                correlation_id=message.meta.reference,
            ),
        )

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aclose__(self, *args, **kwargs):
        await self.close()
