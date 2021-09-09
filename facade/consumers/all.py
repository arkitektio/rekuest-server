

from delt.messages.postman.unreserve.bounced_unreserve import BouncedUnreserveMessage
from delt.messages.postman.unreserve.unreserve import UnreserveMessage
from asgiref.sync import sync_to_async
from facade.consumers.postman import create_assignation_from_bouncedassign, create_bounced_assign_from_assign, create_bounced_reserve_from_reserve, create_bounced_unreserve_from_unreserve, create_reservation_from_bouncedreserve, get_channel_for_reservation
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
import aiormq
from delt.messages.exception import ExceptionMessage
from delt.messages.base import MessageModel
from delt.messages import ReserveMessage, AgentConnectMessage, BouncedAssignMessage, AssignMessage
from channels.generic.websocket import AsyncWebsocketConsumer
from delt.messages.utils import expandFromRabbitMessage, expandToMessage, MessageError
import json
import logging
from herre.bouncer.utils import bounced_ws
import asyncio
from ..models import Provider, Provision
from facade.subscriptions.provider import ProvidersEvent
from facade.enums import ProvisionStatus
from arkitekt.console import console

logger = logging.getLogger(__name__)

def activate_provider_and_get_active_provisions(app, user):

    if user is None or user.is_anonymous:
        provider = Provider.objects.get(app=app, user=None)
    else:
        provider = Provider.objects.get(app=app, user=user)

    provider.active = True
    provider.save()

    if provider.user: 
        ProvidersEvent.broadcast({"action": "started", "data": str(provider.id)}, [f"providers_user_{provider.user.id}"])
    else: 
        ProvidersEvent.broadcast({"action": "started", "data": provider.id}, [f"all_providers"])
    
    provisions = Provision.objects.filter(template__provider=provider).exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED]).all()

    requests = []
    for prov in provisions:
        requests.append(prov.to_message())

    print(requests)
    return provider, requests



class AllConsumer(AsyncWebsocketConsumer):
    mapper = {
        AssignMessage: lambda cls: cls.on_assign,
        BouncedAssignMessage: lambda cls: cls.on_bounced_assign,

        ReserveMessage: lambda cls: cls.on_reserve,
        UnreserveMessage: lambda cls: cls.on_unreserve,
    }

    def __init__(self, *args, **kwargs):
        self.channel = None # The connection layer will be async set by the provider
        assert self.mapper is not None; "Cannot instatiate this Consumer without a Mapper"
        super().__init__(*args, **kwargs)

    @bounced_ws(only_jwt=True)
    async def connect(self):
        logger.error(f"Connecting Postman {self.scope['user']}")
        await self.accept()
        self.callback_name, self.progress_name = await self.connect_to_rabbit()
        self.user = self.scope["user"]
        
        self.reservations_channel_map = {} # Reservations that have been created by this Postman instance
        self.external_reservation_channel_map = {} # Reservations that have NOT been created by this Postman instance


        self.assignations_channel_map = {}


    async def catch(self, text_data, exception=None):
        raise NotImplementedError(f"Received untyped request {text_data}: {exception}")

    async def send_message(self, message: MessageModel):
        await self.send(text_data=message.to_channels())


    async def bounced_reserve(self, bounced_reserve: BouncedReserveMessage):
        bounced_reserve.meta.extensions.callback = self.callback_name
        bounced_reserve.meta.extensions.progress = self.progress_name

        await sync_to_async(create_reservation_from_bouncedreserve)(bounced_reserve)
        await self.forward(bounced_reserve, "bounced_reserve_in")

    async def on_bounced_reserve(self, bounced_reserve: BouncedReserveMessage):
        await self.bounced_reserve(bounced_reserve)

    async def on_reserve(self, reserve: ReserveMessage):
        logger.info("Nanana")
        bounced_reserve: BouncedReserveMessage = await create_bounced_reserve_from_reserve(reserve, self.scope["auth"], self.callback_name, self.progress_name)
        await self.bounced_reserve(bounced_reserve)

    # Bounced Unreserve
    async def bounced_unreserve(self, bounced_unreserve: BouncedUnreserveMessage):
        bounced_unreserve.meta.extensions.callback = self.callback_name
        bounced_unreserve.meta.extensions.progress = self.progress_name
        await self.forward(bounced_unreserve, "bounced_unreserve_in")

    async def on_bounced_unreserve(self, bounced_unreserve: BouncedUnreserveMessage):
        await self.bounced_unreserve(bounced_unreserve)

    async def on_unreserve(self, unreserve: UnreserveMessage):
        bounced_unreserve: BouncedUnreserveMessage = await create_bounced_unreserve_from_unreserve(unreserve, self.scope["auth"], self.callback_name, self.progress_name)
        await self.bounced_unreserve(bounced_unreserve)


    async def bounced_assign(self, bounced_assign: BouncedAssignMessage):
        bounced_assign.meta.extensions.callback = self.callback_name
        bounced_assign.meta.extensions.progress = self.progress_name
        if bounced_assign.meta.extensions.persist:
            await sync_to_async(create_assignation_from_bouncedassign)(bounced_assign)


        reservation = bounced_assign.data.reservation
        
        if reservation not in self.reservations_channel_map:
            if reservation not in self.external_reservation_channel_map:
                logger.info(f"Lets get the assign for that reservation {reservation}")
                self.external_reservation_channel_map[reservation] = await sync_to_async(get_channel_for_reservation)(reservation)
                channel = self.external_reservation_channel_map[reservation]
            else:
                channel = self.external_reservation_channel_map[reservation]
        else:       
            channel = self.reservations_channel_map[reservation]

        logger.info(f"Automatically forwarding it to reservation topic {channel}")

        # We acknowled that this assignation is now linked to the topic (for indefintely)
        self.assignations_channel_map[bounced_assign.meta.reference] = channel
        await self.forward(bounced_assign, channel)

    async def on_assign(self, assign: AssignMessage):
        bounced_assign: AssignMessage = await create_bounced_assign_from_assign(assign,  self.scope["auth"], self.callback_name, self.progress_name)
        console.print(f"[red]{bounced_assign}")
        await self.bounced_assign(bounced_assign)


    async def on_bounced_assign(self, bounced_assign: BouncedAssignMessage):
        await self.bounced_assign(bounced_assign)


    async def on_message_in(self, message):
        expanded_message = expandFromRabbitMessage(message)
        await self.send_message(expanded_message)

    

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.callback_queue = await self.channel.queue_declare(auto_delete=True)
        self.progress_queue = await self.channel.queue_declare(auto_delete=True)

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.callback_queue.queue, self.on_message_in)
        await self.channel.basic_consume(self.progress_queue.queue, self.on_message_in)

        return self.callback_queue.queue, self.progress_queue.queue


    async def forward(self, message: MessageModel, routing_key):
        """Forwards the message to our provessing layer

        Args:
            message (MessageModel): [description]
            routing_key ([type]): The Routing Key (Topic or somethign)
        """

        if routing_key:

            await self.channel.basic_publish(
                    message.to_message(), routing_key=routing_key,
                    properties=aiormq.spec.Basic.Properties(
                        correlation_id=message.meta.reference
            )
            )
        
        else:
            logger.error(f"NO ROUTING KEY SPECIFIED {message}")


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

