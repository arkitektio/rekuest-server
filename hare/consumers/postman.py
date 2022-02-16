from readline import replace_history_item
import ujson
import aiormq
from channels.generic.websocket import AsyncWebsocketConsumer
from lok.bouncer.utils import bounced_ws
import logging
from asgiref.sync import sync_to_async
from facade.models import Waiter, Registry
from hare.consumers.connection import rmq
from urllib.parse import parse_qs
from hare.consumers.helpers import (
    assign,
    list_reservations,
    reserve,
    unassign,
    unreserve,
)
from hare.consumers.messages import (
    AssignPub,
    ReserveList,
    ReservePub,
    ReserveSubUpdate,
    UnassignPub,
    UnreservePub,
)
import asyncio
from arkitekt.console import console

logger = logging.getLogger(__name__)


class PostmanConsumer(AsyncWebsocketConsumer):
    waiter: Waiter

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waiter = None

    @sync_to_async
    def set_waiter(self):
        self.app, self.user = self.scope["bounced"].app, self.scope["bounced"].user
        instance_id = parse_qs(self.scope["query_string"])[b"instance_id"][0].decode(
            "utf8"
        )
        print(instance_id)

        if self.user is None or self.user.is_anonymous:
            registry, _ = Registry.objects.get_or_create(user=None, app=self.app)
        else:
            registry, _ = Registry.objects.get_or_create(user=self.user, app=self.app)

        self.waiter, _ = Waiter.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.set_waiter()
        self.queue_length = 10
        self.incoming_queue = asyncio.Queue(maxsize=self.queue_length)
        self.incoming_task = asyncio.create_task(self.consumer())
        return await super().connect()

    async def receive(self, text_data=None, bytes_data=None):
        self.incoming_queue.put_nowait(
            text_data
        )  # We are buffering here and raise an exception if postman is producing to fast

    async def consumer(self):
        try:
            while True:
                text_data = await self.incoming_queue.get()
                json_dict = ujson.loads(text_data)
                type = json_dict["type"]
                if type == "RESERVE":
                    await self.on_reserve(ReservePub(**json_dict))
                if type == "RESERVE_LIST":
                    await self.on_list_reservations(ReserveList(**json_dict))
                if type == "UNRESERVE":
                    await self.on_unreserve(UnreservePub(**json_dict))
                if type == "ASSIGN":
                    await self.on_assign(AssignPub(**json_dict))
                if type == "UNASSIGN":
                    await self.on_unassign(UnassignPub(**json_dict))

        except Exception as e:
            print(e)

    async def on_reserve(self, message):
        raise NotImplementedError("Error on this")

    async def on_assign(self, message):
        raise NotImplementedError("Error on this")

    async def on_unassign(self, message):
        raise NotImplementedError("Error on this")

    async def on_unreserve(self, message):
        raise NotImplementedError("Error on this")

    async def on_list_reservations(self, message):
        raise NotImplementedError("Error on this")


class HarePostmanConsumer(PostmanConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def connect(self):
        await super().connect()
        self.channel = await rmq.open_channel()

        self.callback_queue = await self.channel.queue_declare(
            f"waiter_{self.waiter.unique}", auto_delete=True
        )

        print(f"Listening on 'waiter_{self.waiter.unique}'")
        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(
            self.callback_queue.queue, self.on_rmq_message_in
        )

    async def on_rmq_message_in(self, rmq_message: aiormq.abc.DeliveredMessage):
        try:
            json_dict = ujson.loads(rmq_message.body)
            type = json_dict["type"]
            if type == "RESERVE_UPDATE":
                m = ReserveSubUpdate(**json_dict)
                await self.send(text_data=m.json())

        except Exception as e:
            console.print_exception()

        self.channel.basic_ack(rmq_message.delivery.delivery_tag)

    async def on_reserve(self, message: ReservePub):

        replies, forwards = await reserve(message, waiter=self.waiter)

        for e in replies:
            await self.send(text_data=e.json())

    async def on_unreserve(self, message: UnreservePub):

        replies, forwards = await unreserve(message, waiter=self.waiter)

        for e in replies:
            await self.send(text_data=e.json())

    async def on_assign(self, message: AssignPub):

        replies, forwards = await assign(message, waiter=self.waiter)

        for e in replies:
            await self.send(text_data=e.json())

    async def on_unassign(self, message: UnassignPub):

        replies, forwards = await unassign(message, waiter=self.waiter)

        for e in replies:
            await self.send(text_data=e.json())

    async def on_list_reservations(self, message: ReserveList):

        replies, forwards = await list_reservations(message, waiter=self.waiter)

        for e in replies:
            await self.send(text_data=e.json())
