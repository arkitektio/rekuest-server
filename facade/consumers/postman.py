from django.contrib.auth import get_user_model
from facade.subscriptions.assignation import MyAssignationsEvent
from facade.subscriptions.reservation import MyReservationsEvent
from herre.bouncer.bounced import Bounced
from herre.models import HerreApp, HerreUser
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
from arkitekt.console import console
logger = logging.getLogger(__name__)

def create_context_from_bounced(bounce: Bounced):
    return {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.email if bounce.user else None,
            "app": bounce.app.client_id if bounce.app else None
    }


def create_assignation_from_bouncedassign(bounced_assign: BouncedAssignMessage):
    context = bounced_assign.meta.context
    extensions = bounced_assign.meta.extensions

    ass = Assignation.objects.create(**{
                "reservation": Reservation.objects.get(reference=bounced_assign.data.reservation),
                "args": bounced_assign.data.args,
                "kwargs": bounced_assign.data.kwargs,
                "context": context.dict(),
                "reference": bounced_assign.meta.reference,
                "creator": HerreUser.objects.get(email=context.user),
                "app": HerreApp.objects.get(client_id=context.app),
                "callback": extensions.callback,
                "progress": extensions.progress
            })

    MyAssignationsEvent.broadcast({"action": "created", "data": ass.id}, [f"assignations_user_{context.user}"])


def create_reservation_from_bouncedreserve(bounced_reserve: BouncedReserveMessage):
    context = bounced_reserve.meta.context
    extensions = bounced_reserve.meta.extensions
    logger.error(f"NOINAOINWAOINWAOINWAOIWNAOIN {bounced_reserve.data.provision}")

    res = Reservation.objects.create(**{
            "node_id": bounced_reserve.data.node,
            "template_id": bounced_reserve.data.template,
            "params": bounced_reserve.data.params.dict() if bounced_reserve.data.params else {},
            "context": context.dict(),
            "reference": bounced_reserve.meta.reference,
            "causing_provision": Provision.objects.get(reference=bounced_reserve.data.provision) if bounced_reserve.data.provision else None,
            "creator": HerreUser.objects.get(email=context.user),
            "app": HerreApp.objects.get(client_id=context.app),
            "callback": extensions.callback,
            "progress": extensions.progress
        })

    MyReservationsEvent.broadcast({"action": "created", "data": res.id}, [f"reservations_user_{context.user}"])


async def create_bounced_assign_from_assign(assign: AssignMessage, bounce: Bounced, callback, progress) -> BouncedAssignMessage:

    bounced_assign= BouncedAssignMessage(data=assign.data, meta={
        "reference": assign.meta.reference,
        "context": create_context_from_bounced(bounce),
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
    })

    return bounced_assign

async def create_bounced_unassign_from_unassign(unassign: UnassignMessage, bounce: Bounced, callback, progress) -> BouncedUnassignMessage:

    bounced_cancel_assign = BouncedUnassignMessage(data=unassign.data, meta={
        "reference": unassign.meta.reference,
        "context": create_context_from_bounced(bounce),
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
    })

    return bounced_cancel_assign

async def create_bounced_reserve_from_reserve(reserve: ReserveMessage, bounce: Bounced, callback, progress) -> BouncedReserveMessage:

    bounced = BouncedReserveMessage(data= reserve.data, meta= {
        "reference": reserve.meta.reference,
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
        "context": create_context_from_bounced(bounce),
    })

    return bounced


async def create_bounced_unreserve_from_unreserve(unreserve: UnreserveMessage, bounce: Bounced, callback, progress) -> BouncedUnreserveMessage:

    bounced = BouncedUnreserveMessage(data= {
        "reservation": unreserve.data.reservation,
    }, meta= {
        "reference": unreserve.meta.reference,
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
        "context": create_context_from_bounced(bounce),
    })

    return bounced


def get_channel_for_reservation(reservation_reference: str) -> str:
    reservation = Reservation.objects.get(reference=reservation_reference)
    return f"assignments_in_{reservation.channel}"


class PostmanConsumer(BaseConsumer):
    mapper = {
        AssignMessage: lambda cls: cls.on_assign,
        UnassignMessage: lambda cls: cls.on_unassign,
        ReserveMessage: lambda cls: cls.on_reserve,
        UnreserveMessage: lambda cls: cls.on_unreserve,

        BouncedReserveMessage: lambda cls: cls.on_bounced_reserve,
        BouncedUnassignMessage: lambda cls: cls.on_bounced_unassign,
        BouncedUnreserveMessage: lambda cls: cls.on_bounced_unreserve,
        BouncedAssignMessage: lambda cls: cls.on_bounced_assign,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        logger.error(f"Connecting Postman {self.scope['user']}")
        await self.accept()
        self.callback_name, self.progress_name = await self.connect_to_rabbit()
        self.user = self.scope["user"]
        
        self.reservations_channel_map = {} # Reservations that have been created by this Postman instance
        self.external_reservation_channel_map = {} # Reservations that have NOT been created by this Postman instance


        self.assignations_channel_map = {} #Saves a local copy of the assignation created by this Postman instance as long as they are running
        
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

    
    async def on_message_in(self, message):
        expanded_message = expandFromRabbitMessage(message)

        if isinstance(expanded_message, ReserveActiveMessage):
            channel = await sync_to_async(get_channel_for_reservation)(expanded_message.meta.reference)
            self.reservations_channel_map[expanded_message.meta.reference] = channel
            logger.info(f"Saving the Topic {channel} for the new Reservation")

        if isinstance(expanded_message, UnreserveDoneMessage):
            self.reservations_channel_map.pop(expanded_message.data.reservation)
            logger.info("Deleting the Topic for the old Reservation")

        if isinstance(expanded_message, AssignDoneMessage):
            # Once we deleted an Assignation we can pop it from the queue
            self.assignations_channel_map.pop(expanded_message.meta.reference)

        if isinstance(expanded_message, UnassignDoneMessage):
            # Once we deleted an Assignation we can pop it from the queue
            self.assignations_channel_map.pop(expanded_message.data.assignation)

        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)
        

    # Reserve Part
    #
    # only if bounced reserve is possible
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


    # Assign
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


    async def bounced_unassign(self, bounced_unassign: BouncedUnassignMessage):
        bounced_unassign.meta.extensions.callback = self.callback_name
        bounced_unassign.meta.extensions.progress = self.progress_name
        topic = self.assignations_channel_map[bounced_unassign.data.assignation]
        logger.warning(f"Automatically forwarding Unassignment to reservation topic {topic}")
        await self.forward(bounced_unassign, topic)



    async def on_unassign(self, unassign: UnassignMessage):
        bounced_unassign: BouncedUnassignMessage = await create_bounced_unassign_from_unassign(unassign,  self.scope["auth"], self.callback_name, self.progress_name)
        await self.bounced_unassign(bounced_unassign)


    async def on_bounced_unassign(self, bounced_unassign: BouncedUnassignMessage):
        await self.bounced_unassign(bounced_unassign)




    async def disconnect(self, close_code):
        logger.info(f"Disconnecting Postman {close_code}")
        # TODO: Implement auto deletion of ongoing assignations
        await self.connection.close()




            

