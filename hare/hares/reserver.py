from facade.enums import PodStatus
from delt.messages.postman.provide.provide_progress import ProgressLevel
from delt.messages.postman.reserve.reserve_done import ReserveDoneMessage
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages.postman.reserve import BouncedReserveMessage, BouncedCancelReserveMessage, ReserveProgressMessage
from delt.messages.postman.provide import BouncedProvideMessage, BouncedCancelProvideMessage
from typing import Tuple
from facade.models import Pod, Provision, Reservation, Template
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

        pod = template.pods.filter(status=PodStatus.ACTIVE).first()

        if pod:
            pod.reservations.add(Reservation.objects.get(reference=reserve.meta.reference))
            return pod.channel

        else:
            return None



@sync_to_async
def create_bouncedprovide_from_bouncedreserve_and_template(bounced_reserve: BouncedReserveMessage, template: Template) -> str:

    provision = Provision.objects.create(
        template= template,
        params = bounced_reserve.data.params.dict(),
        # as we are creating this through a reservation we want the reservation to be callbacked not the provision originating from the hare
        callback = None,
        progress = None,

        creator_id = bounced_reserve.meta.token.user,
        reservation = Reservation.objects.get(reference= bounced_reserve.meta.reference)
    )

    print(provision.reference)

    provide_message = BouncedProvideMessage(data= {
                    "template": template.id,
                    "params": bounced_reserve.data.params,
                },
                meta= {
                    "reference": str(provision.reference),
                    "extensions": bounced_reserve.meta.extensions,
                    "token": bounced_reserve.meta.token
    })

    return provide_message



@sync_to_async
def find_providable_template_for_reservation(reserve: BouncedReserveMessage) -> str:
    ''' Finds a providable template for this reservation and checks the permissions to autoprovide it'''
    params = reserve.data.params
    node = reserve.data.node
    template = reserve.data.template

    if template:
        template = Template.objects.select_related("provider").get(id=template)

    if node:
        qs = Template.objects.select_related("provider").filter(node_id=node)

        if params.providers:
            qs.filter(provider__name_in=params.providers)
        
        # TODO: Check if this is okay

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods
        template =  qs.first()
        
        
    
    assert template is not None, f"Did not find an Template for this Node {reserve.data.node} with params {reserve.data.params}"
    assert template.provider.active == True, "Provider is not active, cannot autoprovide"
    
    return template




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
    async def on_bounced_reserve_in(self, bounced_reserve: BouncedReserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Reserve {str(message.body.decode())}")

        try:
            channel = await find_channel_for_reservation(bounced_reserve)
            if channel: 
                logger.warn(f"Reservation done: channel {channel}")
                # We are routing it to the Template channel (pods will pick up and then reply to)
                reserve_done =  ReserveDoneMessage(data={"channel": channel}, meta={"reference": bounced_reserve.meta.reference})
                await self.forward(reserve_done, bounced_reserve.meta.extensions.callback)

            else:
                logger.info(f"Seeing if autoprovide works for {bounced_reserve.meta.token}")

                template = await find_providable_template_for_reservation(bounced_reserve)

                bounced_provide = await create_bouncedprovide_from_bouncedreserve_and_template(bounced_reserve, template)
                logger.info(bounced_provide)
                reserve_progress =  ReserveProgressMessage(data={"level": ProgressLevel.INFO, "message": f"Providing on {template.provider}"}, meta={"reference": bounced_reserve.meta.reference})
                logger.info(reserve_progress)
                await self.forward(reserve_progress, bounced_reserve.meta.extensions.progress)
                await self.forward(bounced_provide, f"provision_in_{template.provider.unique}")
                
               
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, bounced_reserve.meta.reference)
            await self.forward(exception, bounced_reserve.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)





