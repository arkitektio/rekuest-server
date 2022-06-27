from hare.consumers.postman.base import PostmanConsumer
from hare.consumers.postman.hare.connection import rmq
from hare.consumers.postman.protocols.postman_json import *
import aiormq
import ujson
from .helpers import *
from arkitekt.console import console
import logging

logger = logging.getLogger(__name__)


class HarePostmanConsumer(PostmanConsumer):
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

        await self.channel.exchange_declare(
            exchange=self.waiter.queue, exchange_type="fanout"
        )

        self.callback_queue = await self.channel.queue_declare(exclusive=True)
        await self.channel.queue_bind(
            exchange=self.waiter.queue, queue=self.callback_queue.queue
        )

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(
            self.callback_queue.queue, self.on_rmq_message_in
        )

        logger.debug(f"Listening on '{ self.waiter.queue}'")

    async def forward(self, f: HareMessage):
        logger.debug(f"POSTMAN FORWARDING: {f}")
        await self.channel.basic_publish(
            f.to_message(),
            routing_key=f.queue,  # Lets take the first best one
        )

    async def on_rmq_message_in(self, rmq_message: aiormq.abc.DeliveredMessage):
        try:
            json_dict = ujson.loads(rmq_message.body)
            print("Reveiced on Waiter")
            type = json_dict.pop("type")
            print(type)
            if type == HareMessageTypes.RESERVE_CHANGED:
                m = ReserveSubUpdate(**json_dict)
                print(m)
                await self.reply(m)
            if type == HareMessageTypes.ASSIGN_CHANGED:
                m = AssignSubUpdate(**json_dict)
                print(m)
                await self.reply(m)

            if type == PostmanSubMessageTypes.ASSIGN_UPDATE:
                m = AssignSubUpdate(**json_dict)
                await self.reply(m)

        except Exception:
            console.print_exception()

        self.channel.basic_ack(rmq_message.delivery.delivery_tag)

    async def on_reserve(self, message: ReservePub):

        replies, forwards = await reserve(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)

        for f in forwards:
            await self.forward(f)

    async def on_unreserve(self, message: UnreservePub):

        replies, forwards = await unreserve(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)

        for f in forwards:
            await self.forward(f)

    async def on_assign(self, message: AssignPub):

        replies, forwards = await assign(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)

        for f in forwards:
            print(f)
            await self.forward(f)

    async def on_unassign(self, message: UnassignPub):

        replies, forwards = await unassign(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)

        for f in forwards:
            await self.forward(f)

    async def on_list_reservations(self, message: ReserveList):

        replies, forwards = await list_reservations(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)

    async def on_list_assignations(self, message: AssignList):

        replies, forwards = await list_assignations(message, waiter=self.waiter)

        for r in replies:
            await self.reply(r)
