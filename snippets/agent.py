from readline import replace_history_item
from pydantic import BaseModel
import ujson
import aiormq
from channels.generic.websocket import AsyncWebsocketConsumer
from lok.bouncer.utils import bounced_ws
import logging
from asgiref.sync import sync_to_async
from facade.models import Agent, Assignation, Waiter, Registry
from hare.consumers.agent_message import (
    AgentMessageTypes,
    AssignationsList,
    ProvisionChangedMessage,
    ProvisionList,
)
from hare.connection import rmq
from urllib.parse import parse_qs
from hare.consumers.agent_helpers import (
    change_provision,
    list_assignations,
    list_provisions,
)
from hare.consumers.messages import (
    AssignList,
    AssignPub,
    AssignSubUpdate,
    JSONMessage,
    RPCMessageTypes,
    ReserveList,
    ReservePub,
    ReserveSubUpdate,
    SubMessageTypes,
    UnassignPub,
    UnreservePub,
)
import asyncio
from arkitekt.console import console

logger = logging.getLogger(__name__)


class AgentConsumer(AsyncWebsocketConsumer):
    agent: Agent

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = None

    @sync_to_async
    def set_agent(self):
        self.app, self.user = self.scope["bounced"].app, self.scope["bounced"].user
        instance_id = parse_qs(self.scope["query_string"])[b"instance_id"][0].decode(
            "utf8"
        )
        print(instance_id)

        if self.user is None or self.user.is_anonymous:
            registry, _ = Registry.objects.get_or_create(user=None, app=self.app)
        else:
            registry, _ = Registry.objects.get_or_create(user=self.user, app=self.app)

        self.agent, _ = Agent.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.set_agent()
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
                if type == AgentMessageTypes.LIST_PROVISIONS:
                    await self.on_list_provisions(ProvisionList(**json_dict))
                if type == AgentMessageTypes.LIST_ASSIGNATIONS:
                    await self.on_list_assignations(AssignationsList(**json_dict))

                if type == AgentMessageTypes.PROVIDE_CHANGED:
                    await self.on_provision_changed(
                        ProvisionChangedMessage(**json_dict)
                    )

        except Exception as e:
            print(e)

    async def reply(self, m: JSONMessage):  #
        await self.send(text_data=m.json())

    async def on_list_provisions(self, message):
        raise NotImplementedError("Error on this")

    async def on_list_assignations(self, message):
        raise NotImplementedError("Error on this")


class HareAgentConsumer(AgentConsumer):
    """Hare Postman

    Hare is the default Resolver for the Message Layer using rabbitmq
    it inherits the default



    Args:
        PostmanConsumer (_type_): _description_
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def connect(self):
        await super().connect()
        self.channel = await rmq.open_channel()

        self.callback_queue = await self.channel.queue_declare(
            self.agent.queue, auto_delete=True
        )

        print(f"Liustenting Agent on '{self.agent.queue}'")
        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(
            self.callback_queue.queue, self.on_rmq_message_in
        )

    async def forward(self, f: JSONMessage):
        pass

    async def on_rmq_message_in(self, rmq_message: aiormq.abc.DeliveredMessage):
        try:
            json_dict = ujson.loads(rmq_message.body)
            type = json_dict["type"]
            print(json_dict)

        except Exception as e:
            console.print_exception()

        self.channel.basic_ack(rmq_message.delivery.delivery_tag)

    async def on_list_provisions(self, message: ProvisionList):

        replies, forwards = await list_provisions(message, agent=self.agent)

        for r in replies:
            await self.reply(r)

    async def on_list_assignations(self, message: AssignationsList):

        replies, forwards = await list_assignations(message, agent=self.agent)

        for r in replies:
            await self.reply(r)

    async def on_provision_changed(self, message: ProvisionChangedMessage):

        replies, forwards = await change_provision(message, agent=self.agent)

        for r in replies:
            await self.reply(r)
