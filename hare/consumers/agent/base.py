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
from django.conf import settings
logger = logging.getLogger(__name__)
from hare.consumers.agent.hare.helpers import *


THIS_INSTANCE_NAME = settings.INSTANCE_NAME # corresponds to the hostname
KICKED_CLOSE = 3001
BUSY_CLOSE = 3002
BLOCKED_CLOSE = 3003
BOUNCE_CODE = 3004

class AgentBlocked(Exception):
    pass

class AgentKicked(Exception):
    pass

class AgentBusy(Exception):
    pass



denied_codes = [BUSY_CLOSE] # These are codes that should not change the state of the agent


class AgentConsumer(AsyncWebsocketConsumer):
    agent: Agent

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = None

    @sync_to_async
    def set_agent(self):
        self.client, self.user = self.scope["bounced"].client, self.scope["bounced"].user
        instance_id = (
            parse_qs(self.scope["query_string"])
            .get(b"instance_id", [b"default"])[0]
            .decode("utf8")
        )

        if self.user is None or self.user.is_anonymous:
            registry, _ = Registry.objects.get_or_create(user=None, client=self.client)
        else:
            registry, _ = Registry.objects.get_or_create(user=self.user, client=self.client)

        self.agent, _ = Agent.objects.get_or_create(
            registry=registry, instance_id=instance_id, defaults={"on_instance": THIS_INSTANCE_NAME, "status": AgentStatus.VANILLA}
        )
        if self.agent.status == AgentStatus.ACTIVE:
            raise AgentBusy("Agent already active")
        if self.agent.blocked:
            raise AgentBlocked("Agent blocked. Please unblock it first")

        self.agent.on_instance = THIS_INSTANCE_NAME
        self.agent.status = AgentStatus.ACTIVE
        self.agent.save()

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await super().connect()
        self.queue_length = 5000
        self.incoming_queue = asyncio.Queue(maxsize=self.queue_length)

        try:
            await self.set_agent()
        except AgentBlocked as e:
            logger.error(e)
            await self.close(BLOCKED_CLOSE)
            return
        except AgentBusy as e:
            logger.error(e)
            await self.close(BUSY_CLOSE)
            return
        
        await self.reply(JSONMessage(type=AgentMessageTypes.HELLO))

        replies = await list_provisions(self.agent)
        for reply in replies:
            await self.reply(reply)

        assignations = await list_assignations(self.agent)
        for reply in assignations:
            await self.reply(reply)

        self.incoming_task = asyncio.create_task(self.consumer())

    async def receive(self, text_data=None, bytes_data=None):
        self.incoming_queue.put_nowait(
            text_data
        )  # We are buffering here and raise an exception if postman is producing to fast



    async def on_kick(self, message: str):
        print("KICKING")
        await self.close(KICKED_CLOSE)

    async def on_bounce(self, message: str):
        print("IS BOUNCED")
        await self.close(BOUNCE_CODE)




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
            logger.critical("Critical Error in handling message in constumer", exc_info=e)
            await self.close(4001)
            raise e

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
