import ujson
from channels.generic.websocket import AsyncWebsocketConsumer
from lok.bouncer.utils import bounced_ws
import logging
from asgiref.sync import sync_to_async
from facade.models import Agent, Registry
from facade.enums import AgentStatus
from hare.consumers.agent.protocols.agent_json import *
from urllib.parse import parse_qs
import asyncio

logger = logging.getLogger(__name__)


class AgentConsumer(AsyncWebsocketConsumer):
    agent: Agent

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = None

    @sync_to_async
    def set_agent(self):
        self.app, self.user = self.scope["bounced"].app, self.scope["bounced"].user
        instance_id = (
            parse_qs(self.scope["query_string"])
            .get(b"instance_id", [b"default"])[0]
            .decode("utf8")
        )

        if self.user is None or self.user.is_anonymous:
            registry, _ = Registry.objects.get_or_create(user=None, app=self.app)
        else:
            registry, _ = Registry.objects.get_or_create(user=self.user, app=self.app)

        self.agent, _ = Agent.objects.get_or_create(
            registry=registry, identifier=instance_id
        )

        self.agent.status = AgentStatus.ACTIVE
        self.agent.save()

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.set_agent()
        self.queue_length = 5000
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
                if type == AgentMessageTypes.ASSIGN_CHANGED:
                    await self.on_assignation_changed(
                        AssignationChangedMessage(**json_dict)
                    )

                if type == AgentMessageTypes.ASSIGN_LOG:
                    await self.on_assignation_log(AssignationLogMessage(**json_dict))

                if type == AgentMessageTypes.PROVIDE_LOG:
                    await self.on_provision_log(ProvisionLogMessage(**json_dict))

                self.incoming_queue.task_done()

        except Exception as e:
            logger.exception(e)

    async def reply(self, m: JSONMessage):  #
        await self.send(text_data=m.json())

    async def on_list_provisions(self, message):
        raise NotImplementedError("Error on this")

    async def on_provision_changed(self, message):
        raise NotImplementedError("Error on this")

    async def on_assignation_changed(self, message):
        raise NotImplementedError("Error on this")

    async def on_assignation_log(self, message):
        raise NotImplementedError("Error on this")

    async def on_provision_log(self, message):
        raise NotImplementedError("Error on this")
