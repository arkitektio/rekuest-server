from delt.messages.postman.reserve.reserve_done import ReserveDoneMessage
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages.postman.reserve import BouncedReserveMessage, BouncedCancelReserveMessage
from typing import Tuple
from facade.models import Pod, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare

logger = logging.getLogger(__name__)


@sync_to_async
def find_channel_for_reservation(reserve: BouncedReserveMessage) -> str:

    params = reserve.data.params
    node = reserve.data.node
    template = reserve.data.template

    if node:
        qs = Template.objects.select_related("provider").filter(node_id=node)

        if params.providers:
            qs.filter(provider__name_in=params.providers)

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods
        template =  qs.first()
        assert template is not None, f"Did not find an Template for this Node {reserve.data.node} with params {reserve.data.params}"

        if template.is_active:
            return  template.pods.order_by("-id").first().channel
        else:
            return "Fake Channel"



    raise Exception("You did not provide a node never happen")




class ReserverRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.bounced_reserve_in = await self.channel.queue_declare("bounced_reserve_in")
        self.bounced_cancel_provide_in = await self.channel.queue_declare("bounced_cancel_provide_in")


        # We will get Results here
        self.provision_done = await self.channel.queue_declare("provision_done")

        # Start listening the queue with name 'hello'

        await self.channel.basic_consume(self.bounced_reserve_in.queue, self.on_bounced_reserve_in)
        await self.channel.basic_consume(self.bounced_cancel_provide_in.queue, self.on_bounced_cancel_provide_in)

    @BouncedCancelReserveMessage.unwrapped_message
    async def on_bounced_cancel_provide_in(self, cancel_provide: BouncedCancelReserveMessage, message: aiormq.types.DeliveredMessage):
        logger.warn(f"Received Provision Cancellation  {str(message.body.decode())}")
        # Thi should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @BouncedReserveMessage.unwrapped_message
    async def on_bounced_reserve_in(self, bounced_provide: BouncedReserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Reserve {str(message.body.decode())}")

        try:
            channel = await find_channel_for_reservation(bounced_provide)
            if channel: 
                logger.warn(f"Reservation done: channel {channel}")
                # We are routing it to the Template channel (pods will pick up and then reply to)
                reserve_done =  ReserveDoneMessage(data={"channel": channel}, meta={"reference": bounced_provide.meta.reference})
                await self.forward(reserve_done, bounced_provide.meta.extensions.callback)

            else:
                logger.warn(f"Autprovision route because we couldn't get an exisitng topic we could assign to")
                raise NotImplementedError("Autoprovision through reservartion is not implememnted yet. Please provide the infrastructure before you continue")

        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, bounced_provide.meta.reference)
            await self.forward(exception, bounced_provide.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)





