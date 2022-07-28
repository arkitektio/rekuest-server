from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from delt.messages.postman.unprovide.bounced_unprovide import BouncedUnprovideMessage
from delt.messages.postman.unassign.bounced_unassign import BouncedUnassignMessage
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.unreserve.bounced_unreserve import BouncedUnreserveMessage
from facade.enums import ReservationStatus
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from delt.messages.utils import expandToMessage
import aiormq
from channels.layers import get_channel_layer
from delt.messages.base import MessageModel
from channels.consumer import AsyncConsumer
import logging
from asgiref.sync import async_to_sync
from arkitekt.console import console


channel_layer = get_channel_layer()

logger = logging.getLogger(__name__)


class GatewayConsumer(AsyncConsumer):
    @classmethod
    def send(cls, message: MessageModel):
        async_to_sync(channel_layer.send)(
            "gateway", {"type": "on_django_in", "data": message.dict()}
        )

    def __init__(self) -> None:
        self.callback_name = None
        self.connection = None
        self.progress_name = None
        super().__init__()
        #

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # Declaring queue
        self.callback_queue = await self.channel.queue_declare("gateway_callback")
        self.progress_queue = await self.channel.queue_declare("gateway_progress")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.callback_queue.queue, self.on_rabbit_in)
        await self.channel.basic_consume(self.progress_queue.queue, self.on_rabbit_in)

        self.callback_name = self.callback_queue.queue
        self.progress_name = self.progress_queue.queue

    async def on_rabbit_in(self, aiomessage):
        pass

    async def forward(self, message: MessageModel, routing_key):
        """Forwards the message to our processing layer

        Args:
            message (MessageModel): [description]
            routing_key ([type]): The Routing Key (Topic or somethign)
        """
        console.log("forwarding", message)

        await self.channel.basic_publish(
            message.to_message(),
            routing_key=routing_key,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=message.meta.reference
            ),
        )

    async def on_django_in(self, message):
        if self.connection is None:
            await self.connect_to_rabbit()

        message = expandToMessage(message["data"])
        message.meta.extensions.callback = self.callback_name
        message.meta.extensions.progress = self.progress_name

        if isinstance(message, BouncedReserveMessage):
            await self.forward(message, "bounced_reserve_in")

        if isinstance(message, BouncedProvideMessage):
            await self.forward(message, "bounced_provide_in")

        if isinstance(message, BouncedAssignMessage):
            await self.forward(message, "bounced_assign_in")

        if isinstance(message, BouncedUnassignMessage):
            await self.forward(message, "bounced_unassign_in")

        if isinstance(message, BouncedUnreserveMessage):
            await self.forward(message, "bounced_unreserve_in")

        if isinstance(message, BouncedUnprovideMessage):
            await self.forward(message, "bounced_unprovide_in")
