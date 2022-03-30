from urllib.parse import parse_qs
from hare.consumers.postman.protocols.postman_json import *
from channels.generic.websocket import AsyncWebsocketConsumer
from facade.models import Waiter, Registry
from asgiref.sync import sync_to_async
import ujson
from lok.bouncer.utils import bounced_ws
import asyncio


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
                if type == PostmanMessageTypes.RESERVE:
                    await self.on_reserve(ReservePub(**json_dict))
                if type == PostmanMessageTypes.LIST_RESERVATION:
                    await self.on_list_reservations(ReserveList(**json_dict))
                if type == PostmanMessageTypes.UNRESERVE:
                    await self.on_unreserve(UnreservePub(**json_dict))
                if type == PostmanMessageTypes.ASSIGN:
                    await self.on_assign(AssignPub(**json_dict))
                if type == PostmanMessageTypes.UNASSIGN:
                    await self.on_unassign(UnassignPub(**json_dict))
                if type == PostmanMessageTypes.LIST_ASSIGNATION:
                    await self.on_list_assignations(AssignList(**json_dict))

                self.incoming_queue.task_done()

        except Exception as e:
            print(e)

    async def reply(self, m: JSONMessage):  #
        print("Sending to client")
        await self.send(text_data=m.json())

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

    async def on_list_assignations(self, message):
        raise NotImplementedError("Error on this")
