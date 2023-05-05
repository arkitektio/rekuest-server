import aiormq
from hare.carrots import (
    HareMessage,
    HareMessageTypes,
    ProvideHareMessage,
    ReserveHareMessage,
    UnassignHareMessage,
    UnprovideHareMessage,
    UnreserveHareMessage,
)
from hare.connection import rmq
from hare.consumers.agent.protocols.agent_json import *
import ujson
from arkitekt.console import console
from hare.consumers.agent.base import AgentConsumer
from .helpers import *
import logging

logger = logging.getLogger(__name__)


class HareAgentConsumer(AgentConsumer):
    """Hare Postman

    Hare is the default Resolver for the Message Layer using rabbitmq
    it inherits the default

    Args:
        PostmanConsumer (_type_): _description_
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.res_queues = {}
        self.res_consumers = {}
        self.res_consumer_tags = {}
        self.prov_queues = {}
        self.prov_consumers = {}
        self.prov_consumer_tags = {}
        self.ass_delivery_tags = {}

    async def connect(self):
        await super().connect()
        self.channel = await rmq.open_channel()

        self.callback_queue = await self.channel.queue_declare(
            self.agent.queue, auto_delete=True
        )

        logger.info(f"Liustenting Agent on '{self.agent.queue}'")
        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(
            self.callback_queue.queue, self.on_rmq_message_in
        )

    async def forward(self, f: HareMessage):
        try:
            await self.channel.basic_publish(
                f.to_message(),
                routing_key=f.queue,
            )
        except Exception:
            logger.exception("Error on forward", exc_info=True)

    async def on_rmq_message_in(self, rmq_message: aiormq.abc.DeliveredMessage):
        try:
            json_dict = ujson.loads(rmq_message.body)
            type = json_dict["type"]

            if type == HareMessageTypes.RESERVE:
                await self.on_reserve(ReserveHareMessage(**json_dict))

            if type == HareMessageTypes.UNRESERVE:
                await self.on_unreserve(UnreserveHareMessage(**json_dict))

            if type == HareMessageTypes.PROVIDE:
                await self.on_provide(ProvideHareMessage(**json_dict))

            if type == HareMessageTypes.UNPROVIDE:
                await self.on_unprovide(UnprovideHareMessage(**json_dict))

        except Exception:
            logger.exception("Error on on_rmq_message_ins")

        self.channel.basic_ack(rmq_message.delivery.delivery_tag)

    async def on_list_provisions(self, message: ProvisionList):

        replies, forwards = await list_provisions(message, agent=self.agent)

        for r in replies:
            await self.reply(r)

    async def on_list_assignations(self, message: AssignationsList):

        replies, forwards = await list_assignations(message, agent=self.agent)

        for r in replies:
            await self.reply(r)

    async def on_assignment_in(self, provid, rmq_message):
        replies = []
        forwards = []
        try:
            json_dict = ujson.loads(rmq_message.body)
            type = json_dict["type"]


            if type == HareMessageTypes.ASSIGN:
                m = AssignHareMessage(**json_dict)
                self.ass_delivery_tags[m.assignation] = rmq_message.delivery.delivery_tag
                replies, forwards = await bind_assignation(m, provid)

            if type == HareMessageTypes.UNASSIGN:
                m = UnassignHareMessage(**json_dict)
                self.channel.basic_ack(rmq_message.delivery.delivery_tag)
                replies = [UnassignSubMessage(**json_dict)]

        except Exception:
            logger.error("Error on on_assignment_in", exc_info=True)

        logger.debug(f"Received Assignment for {provid} {rmq_message}")

        for r in replies:
            await self.reply(r)

        for r in forwards:
            await self.forward(r)

        
        logger.error(f"OINOINOINOIN Acknowledged Assignment for {provid} {rmq_message}")

    async def on_provision_changed(self, message: ProvisionChangedMessage):

        if message.status == ProvisionStatus.CANCELLED:
            # TODO: SOmehow acknowled this by logging it
            return

        if message.status == ProvisionStatus.ACTIVE:
            replies, forwards, queues, prov_queue = await activate_provision(
                message, agent=self.agent
            )

            for res, queue in queues:
                logger.error(f"Lisenting for queue of Reservation {res}")
                self.res_queues[res] = await self.channel.queue_declare(
                    queue,
                    auto_delete=True,
                )
                tag =  str(uuid.uuid4())
                self.res_consumers[res] = await self.channel.basic_consume(
                    self.res_queues[res].queue,
                    lambda aio: self.on_assignment_in(message.provision, aio),
                    consumer_tag=tag,
                    no_ack=True,
                )
                self.res_consumer_tags[res] = tag



            tag =  str(uuid.uuid4())
            prov_id, prov_queue = prov_queue
            self.prov_queues[prov_id] = await self.channel.queue_declare(
                prov_queue,
                auto_delete=True,
            )
            self.prov_consumers[prov_id] = await self.channel.basic_consume(
                self.prov_queues[prov_id].queue,
                lambda aio: self.on_assignment_in(message.provision, aio),
                consumer_tag=tag,
                no_ack=True,
            )
            self.prov_consumer_tags[prov_id] = tag

        else:
            replies, forwards = await change_provision(message, agent=self.agent)

        for r in forwards:
            await self.forward(r)

        for r in replies:
            await self.reply(r)

    async def on_assignation_changed(self, message: AssignationChangedMessage):

        if message.status == AssignationStatus.ASSIGNED:
            if message.assignation in self.ass_delivery_tags:
                logger.error("Aknowledging this shit")
                self.channel.basic_ack(self.ass_delivery_tags[message.assignation])
            else:
                logger.error("Unaknowledgeablebl")

        replies, forwards = await change_assignation(message, agent=self.agent)
        logger.info(f"Received Assignation for {message.assignation} {forwards}")

        for r in forwards:
            await self.forward(r)

        for r in replies:
            await self.reply(r)

    async def on_reserve(self, message: ReserveHareMessage):

        replies, forwards, reservation_queues = await accept_reservation(
            message, agent=self.agent
        )

        for res, queue in reservation_queues:
            logger.info(f"Lisenting for queue of Reservation {res}")
            self.res_queues[res] = await self.channel.queue_declare(
                queue, auto_delete=True
            )

            await self.channel.basic_consume(
                self.res_queues[res].queue,
                lambda aio: self.on_assignment_in(message.provision, aio),
                consumer_tag=f"res-{res}-prov-{message.provision}",
            )

        for r in forwards:
            await self.forward(r)

        for r in replies:
            await self.reply(r)

    async def on_unreserve(self, message: UnreserveHareMessage):

        replies, forwards, delete_queue_id = await loose_reservation(
            message, agent=self.agent
        )

        for id in delete_queue_id:
            consumer_tag = self.res_consumer_tags[id]
            await self.channel.basic_cancel(consumer_tag)
            logger.debug(
                f"Deleting consumer for queue {id} of Reservation {message.provision}"
            )

        for r in forwards:
            await self.forward(r)

        for r in replies:
            await self.reply(r)

    async def on_provide(self, message: ProvideHareMessage):
        logger.warning(f"Agent received PROVIDE {message}")

        replies = [
            ProvideSubMessage(
                provision=message.provision,
                guardian=message.provision,
                template=message.template,
                status=message.status,
            )
        ]

        for r in replies:
            await self.reply(r)

    async def on_unprovide(self, message: UnprovideHareMessage):
        logger.warning(f"Agent received UNPROVIDE {message}")

        replies = [
            UnprovideSubMessage(
                provision=message.provision,
            )
        ]

        loose_tag = self.prov_consumer_tags[message.provision]
        await self.channel.basic_cancel(loose_tag)
        logger.debug(
            f"Deleting consumer for queue {id} of Reservation {message.provision}"
        )


        for r in replies:
            await self.reply(r)

    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting Postman with close_code {close_code}")
            # We are deleting all associated Provisions for this Agent
            forwards = await disconnect_agent(self.agent, close_code)

            for r in forwards:
                await self.forward(r)


            await self.channel.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")

    async def on_provision_log(self, message):
        await log_to_provision(message, agent=self.agent)

    async def on_assignation_log(self, message):
        await log_to_assignation(message, agent=self.agent)
