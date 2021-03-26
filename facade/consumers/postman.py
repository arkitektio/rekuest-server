# chat/consumers.py
from os import unsetenv
from delt.messages.postman.reserve.unreserve import UnReserveMessage
from delt.messages.postman import reserve
from delt.messages.postman.reserve.reserve_done import ReserveDoneMessage
from delt.messages.utils import expandFromRabbitMessage
from delt.messages.postman.reserve import ReserveMessage, CancelReserveMessage, BouncedReserveMessage
from herre.bouncer.bounced import Bounced
from delt.messages.postman.provide import BouncedProvideMessage, ProvideMessage, CancelProvideMessage
from delt.messages.postman.assign import CancelAssignMessage, AssignMessage, BouncedAssignMessage
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



@sync_to_async
def create_bounced_assign_from_assign(assign: AssignMessage, bounce: Bounced, callback, progress) -> BouncedAssignMessage:

    
    assignation = Assignation.objects.update_or_create(reference=assign.meta.reference, **{
            "args": assign.data.args,
            "kwargs": assign.data.kwargs,
            "node_id": assign.data.node,
            "pod_id":assign.data.pod,
            "template_id": assign.data.template,
            "creator": bounce.user,
            "callback": callback,
            "progress": progress,
        }
        )

    bounced_assign= BouncedAssignMessage(data=assign.data, meta={
        "reference": assign.meta.reference,
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id
        },
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
    })

    return bounced_assign





@sync_to_async
def create_bounced_provide_from_provide(provide: ProvideMessage, bounce: Bounced, callback, progress) -> BouncedProvideMessage:

    
    provision = Provision.objects.update_or_create(reference=provide.meta.reference, **{
        "template_id": provide.data.template,
        "params": provide.data.params.dict(),
        "creator": bounce.user,
        "callback": callback,
        "progress": progress
    }
    )

    print(provision)

    bounced = BouncedProvideMessage(data= {
        "node": provide.data.node,
        "template": provide.data.template,
        "params": provide.data.params
    }, meta= {
        "reference": provide.meta.reference,
        "extensions": {
            "callback": callback,
            "progress": progress,
        },
        "token": {
            "roles": bounce.roles,
            "scopes": bounce.scopes,
            "user": bounce.user.id
        }
    })

    return bounced


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

    print(reservation)

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
            "user": bounce.user.id
        }
    })

    return bounced


@sync_to_async
def delete_reservation_from_unreserve(unreserve: UnReserveMessage, bounce: Bounced) -> BouncedReserveMessage:

    reservation = Reservation.objects.get(reference=unreserve.data.reservation)
    reservation.delete()
    return True

class PostmanConsumer(BaseConsumer):
    mapper = {
        AssignMessage: lambda cls: cls.on_assign,
        ProvideMessage: lambda cls: cls.on_provide,
        CancelAssignMessage: lambda cls: cls.on_cancel_assign,
        CancelProvideMessage: lambda cls: cls.on_cancel_provide,
        ReserveMessage: lambda cls: cls.on_reserve,
        UnReserveMessage: lambda cls: cls.on_unreserve,
        CancelReserveMessage: lambda cls: cls.on_on_cancel_reserve
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        logger.error(f"Connecting Postman {self.scope['user']}")
        await self.accept()
        self.callback_name, self.progress_name = await self.connect_to_rabbit()
        self.user = self.scope["user"]
        
        self.provisions = {}
        self.reservations = {}
        
    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.callback_queue = await self.channel.queue_declare(auto_delete=True)
        self.progress_queue = await self.channel.queue_declare(auto_delete=True)
        self.reservation_queue = await self.channel.queue_declare(auto_delete=True)

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.callback_queue.queue, self.on_callback)


        await self.channel.basic_consume(self.reservation_queue.queue, self.on_reservation)



        await self.channel.basic_consume(self.progress_queue.queue, self.on_progress)
        return self.callback_queue.queue, self.progress_queue.queue
        

    async def on_callback(self, message):
        text_data = message.body.decode()
        json.loads(text_data)
        logger.warn(f"Sending {text_data} to Postman Callback")


        logger.error(message)
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def on_progress(self, message):
        logger.info(message)

        logger.warn(f"Sending {message} to Postman Progress")
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def disconnect(self, close_code):
        logger.info(f"Disconnecting Postman {close_code}")
        await self.connection.close()


    async def on_assign(self, assign: AssignMessage):
        bounced_assign: AssignMessage = await create_bounced_assign_from_assign(assign,  self.scope["auth"], self.callback_name, self.progress_name)
        print(bounced_assign)

        if bounced_assign.data.reservation in self.reservations:
            channel = self.reservations[bounced_assign.data.reservation].data.channel
            logger.info(f"Automatically forwarding it to reservation channel {channel}")
            await self.forward(bounced_assign, channel)

        else:
            await self.forward(bounced_assign, "bounced_assign_in")

    async def on_provide(self, provide: ProvideMessage):
        provide: BouncedProvideMessage = await create_bounced_provide_from_provide(provide, self.scope["auth"], self.callback_name, self.progress_name)
        await self.forward(provide, "bounced_provide_in")

        # We save the provision so that on a disconnect we can disconnect (TODO: except systemic calls)
        self.provisions[provide.meta.reference] = provide



    async def on_reserve(self, reserve: ReserveMessage):
        provide: BouncedProvideMessage = await create_bounced_reserve_from_reserve(reserve, self.scope["auth"], self.reservation_queue.queue, self.progress_name)
        await self.forward(provide, "bounced_reserve_in")

    async def on_unreserve(self, unreserve: UnReserveMessage):
        
        nana = await delete_reservation_from_unreserve(unreserve, self.scope["auth"])
        if nana: self.reservations.pop(unreserve.data.reservation)
        logger.error("Unreserving {unreserve]")
        #provide: BouncedProvideMessage = await create_bounced_reserve_from_reserve(reserve, self.scope["auth"], self.reservation_queue.queue, self.progress_name)
        #await self.forward(provide, "bounced_reserve_in")

    async def on_reservation(self, message):
        expanded_message = expandFromRabbitMessage(message)
        if isinstance(expanded_message, ReserveDoneMessage):
            self.reservations[expanded_message.meta.reference] = expanded_message
            print("Reservation was done")

        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_cancel_assign(self, cancel_assign: CancelAssignMessage):
        #TODO: Check if assignation exists
        print(cancel_assign)

    async def on_cancel_provide(self, cancel_provide: CancelProvideMessage):
        print(cancel_provide)

    async def on_cancel_reserve(self, cancel_reserve: CancelReserveMessage):
        print(cancel_reserve)





            

