from herre.bouncer.bounced import Bounced
from delt.messages import *
from delt.messages.utils import expandFromRabbitMessage
from facade.models import Assignation, Provision, Reservation
from asgiref.sync import sync_to_async
from facade.consumers.base import BaseConsumer
from herre.bouncer.utils import bounced_ws
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
import aiormq
import logging
from herre.token import JwtToken
import json

logger = logging.getLogger(__name__)



async def create_bounced_assign_from_assign(assign: AssignMessage, bounce: Bounced, callback, progress) -> BouncedAssignMessage:

    bounced_assign= BouncedAssignMessage(data=assign.data, meta={
        "reference": assign.meta.reference,
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id if bounce.user else None
        },
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
    })

    return bounced_assign

@sync_to_async
def create_bounced_unassign_from_unassign(unassign: UnassignMessage, bounce: Bounced, callback, progress) -> BouncedUnassignMessage:

    bounced_cancel_assign = BouncedUnassignMessage(data=unassign.data, meta={
        "reference": unassign.meta.reference,
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id if bounce.user else None
        },
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
    })

    return bounced_cancel_assign



@sync_to_async
def create_bounced_reserve_from_reserve(reserve: ReserveMessage, bounce: Bounced, callback, progress) -> BouncedReserveMessage:

    
    reservation = Reservation.objects.update_or_create(reference=reserve.meta.reference, **{
        "node_id": reserve.data.node,
        "template_id": reserve.data.template,
        "params": reserve.data.params.dict(),
        "creator": bounce.user,
        "callback": callback,
        "progress": progress
    }
    )

    bounced = BouncedReserveMessage(data= {
        "node": reserve.data.node,
        "template": reserve.data.template,
        "params": reserve.data.params
    }, meta= {
        "reference": reserve.meta.reference,
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id if bounce.user else None
        }
    })

    return bounced


@sync_to_async
def create_bounced_unreserve_from_unreserve(unreserve: UnreserveMessage, bounce: Bounced, callback, progress) -> BouncedUnreserveMessage:

    bounced = BouncedUnreserveMessage(data= {
        "reservation": unreserve.data.reservation,
    }, meta= {
        "reference": unreserve.meta.reference,
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id if bounce.user else None
        }
    })

    return bounced


@sync_to_async
def delete_reservation_from_unreserve(unreserve: UnreserveMessage, bounce: Bounced) -> BouncedReserveMessage:

    reservation = Reservation.objects.get(reference=unreserve.data.reservation)
    reservation.delete()
    return True

class PostmanConsumer(BaseConsumer):
    mapper = {
        AssignMessage: lambda cls: cls.on_assign,
        UnassignMessage: lambda cls: cls.on_unassign,
        ReserveMessage: lambda cls: cls.on_reserve,
        UnreserveMessage: lambda cls: cls.on_unreserve,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        logger.error(f"Connecting Postman {self.scope['user']}")
        await self.accept()
        self.callback_name, self.progress_name = await self.connect_to_rabbit()
        self.user = self.scope["user"]
        
        self.reservations_topic_map = {}
        self.assignations_topic_map = {}
        
    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.callback_queue = await self.channel.queue_declare(auto_delete=True)
        self.progress_queue = await self.channel.queue_declare(auto_delete=True)

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.callback_queue.queue, self.on_message_in)
        await self.channel.basic_consume(self.progress_queue.queue, self.on_progress_in)

        return self.callback_queue.queue, self.progress_queue.queue

    
    async def on_message_in(self, message):
        expanded_message = expandFromRabbitMessage(message)

        if isinstance(expanded_message, ReserveDoneMessage):
            self.reservations_topic_map[expanded_message.meta.reference] = expanded_message.data.topic
            logger.info(f"Saving the Topic {expanded_message.data.topic} for the new Reservation")

        if isinstance(expanded_message, UnreserveDoneMessage):
            self.reservations_topic_map.pop(expanded_message.data.reservation)
            logger.info("Deleting the Topic for the old Reservation")

        if isinstance(expanded_message, AssignDoneMessage):
            # Once we deleted an Assignation we can pop it from the queue
            self.assignations_topic_map.pop(expanded_message.meta.reference)

        if isinstance(expanded_message, UnassignDoneMessage):
            # Once we deleted an Assignation we can pop it from the queue
            self.assignations_topic_map.pop(expanded_message.data.assignation)


        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)
        

    async def on_progress_in(self, message):
        # We can seperate this out because progress can just be handed through
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_assign(self, assign: AssignMessage):
        bounced_assign: AssignMessage = await create_bounced_assign_from_assign(assign,  self.scope["auth"], self.callback_name, self.progress_name)

        topic = self.reservations_topic_map[bounced_assign.data.reservation]
        logger.info(f"Automatically forwarding it to reservation topic {topic}")

        # We acknowled that this assignation is now linked to the topic (for indefintely)
        self.assignations_topic_map[bounced_assign.meta.reference] = topic
        await self.forward(bounced_assign, topic)


    async def on_reserve(self, reserve: ReserveMessage):
        provide: BouncedProvideMessage = await create_bounced_reserve_from_reserve(reserve, self.scope["auth"], self.callback_name, self.progress_name)
        await self.forward(provide, "bounced_reserve_in")

    async def on_unreserve(self, unreserve: UnreserveMessage):
        bounced_unreserve: BouncedUnreserveMessage = await create_bounced_unreserve_from_unreserve(unreserve, self.scope["auth"], self.callback_name, self.progress_name)
        await self.forward(bounced_unreserve, "bounced_unreserve_in")

    async def on_unassign(self, unassign: UnassignMessage):
        bounced_cancel_assign: BouncedUnassignMessage = await create_bounced_unassign_from_unassign(unassign,  self.scope["auth"], self.callback_name, self.progress_name)
        topic = self.assignations_topic_map[bounced_cancel_assign.data.assignation]
        logger.warning(f"Automatically forwarding Unassignment to reservation topic {topic}")
        await self.forward(bounced_cancel_assign, topic)

    async def disconnect(self, close_code):
        logger.info(f"Disconnecting Postman {close_code}")
        # TODO: Implement auto deletion of ongoing assignations
        await self.connection.close()




            

