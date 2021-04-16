
from delt.messages.postman.provide.params import ProvideParams
from delt.messages.postman.progress import ProgressLevel
from facade.enums import PodStatus
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages import *
from typing import Tuple, Union
from facade.models import Pod, Provision, Reservation, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare
from arkitekt.console import console
logger = logging.getLogger(__name__)


@sync_to_async
def create_reservation_from_bounced_reserve(reserve: BouncedReserveMessage):
    reservation, created = Reservation.objects.update_or_create(reference=reserve.meta.reference, **{
        "node_id": reserve.data.node,
        "template_id": reserve.data.template,
        "params": reserve.data.params.dict(),
        "creator_id": reserve.meta.token.user,
        "callback": reserve.meta.extensions.callback,
        "progress": reserve.meta.extensions.progress
    })
    return reservation




@sync_to_async
def find_topic_for_reservation(reserve: BouncedReserveMessage, reservation: Reservation) -> str:
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
            pod.reservations.add(reservation)
            return pod.channel

        else:
            return None


@sync_to_async
def find_topic_for_unreservation(unreserve: BouncedUnreserveMessage) -> str:
    reservation = Reservation.objects.get(reference=unreserve.data.reservation)
    return reservation.pod.channel



@sync_to_async
def create_bouncedprovide_from_bouncedreserve_and_template(bounced_reserve: BouncedReserveMessage, template: Template, reservation: Reservation) -> str:

    provision = Provision.objects.create(
        reference= bounced_reserve.meta.reference, # We use the same reference that the user wants
        template= template,
        params = bounced_reserve.data.params.dict(),
        # as we are creating this through a reservation we want the reservation to be callbacked not the provision originating from the hare
        callback = None,
        progress = None,

        creator_id = bounced_reserve.meta.token.user,
        reservation = reservation #Even though we share the same reference??
    )

    provide_message = BouncedProvideMessage(data= {
                    "template": template.id,
                    "params": bounced_reserve.data.params,
                },
                meta= {
                    "reference": provision.reference,
                    "extensions": bounced_reserve.meta.extensions,
                    "token": bounced_reserve.meta.token
    })

    return provide_message



@sync_to_async
def cancel_reservation(bounced_unreserve: BouncedUnreserveMessage) -> Union[Tuple[BouncedUnprovideMessage, str], Tuple[None, None]]:
    ''' Cancels the reservation on a Pod and returns an UnprovideMessage if we are to unprovide the Pod '''


    reservation = Reservation.objects.get(reference = bounced_unreserve.data.reservation)

    pod = reservation.pod
    # Lets check if we are the last reservation for the pod?
    print(pod.reservations.all())
    if pod.reservations.count() == 1:
        # Only if the reservation that created this pod dies are we allowed to unprovide
        if pod.provision.reservation.id == reservation.id:
            params = ProvideParams(**pod.provision.params)
            if params.auto_unprovide:
                return BouncedUnprovideMessage(
                    data={
                        "provision": str(pod.provision.reference)
                    },
                    meta= {
                        "reference": bounced_unreserve.meta.reference,
                        "extensions": bounced_unreserve.meta.extensions,
                        "token": bounced_unreserve.meta.token,
                    }), pod.template.provider

    return None, None
 

@sync_to_async
def get_unprovision_to_cause(unreserve_done: UnreserveDoneMessage):
    ''' Cancels the reservation on a Pod and returns an UnprovideMessage if we are to unprovide the Pod '''


    reservation = Reservation.objects.get(reference = unreserve_done.data.reservation)

    pod = reservation.pod
    # Lets check if we are the last reservation for the pod?
    print(pod.reservations.all())
    if pod.reservations.count() == 1:
        # Only if the reservation that created this pod dies are we allowed to unprovide
        if pod.provision.reservation.id == reservation.id:
            params = ProvideParams(**pod.provision.params)
            if params.auto_unprovide:
                return BouncedUnprovideMessage(
                    data={
                        "provision": str(pod.provision.reference)
                    },
                    meta= {
                        "reference": unreserve_done.meta.reference,
                        "extensions": {}, # This is a system call, we do not need any callback. The disappearing pod is enough!
                        "token": {"scopes": [] , "roles": [], "user": 1 },
                    }), pod.template.provider

    return None, None



@sync_to_async
def find_providable_template_for_reservation(reserve: BouncedReserveMessage, reservation: Reservation) -> str:

    assert reserve.data.params.auto_provide == True, "There is no active Pod for this Node and you didn't provide autoprovide"
    assert "can_provide" in reserve.meta.token.scopes, "Your App does not have the proper permissions set to autoprovide (add [bold]can_provide[/] to your scopes"
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
        self.bounced_unreserve_in = await self.channel.queue_declare("bounced_unreserve_in")
        self.bounced_cancel_provide_in = await self.channel.queue_declare("bounced_cancel_provide_in")
        self.unreserve_done_in = await self.channel.queue_declare("unreserve_done_in")


        # We will get Results here
        self.provision_done = await self.channel.queue_declare("provision_done")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.bounced_reserve_in.queue, self.on_bounced_reserve_in)
        await self.channel.basic_consume(self.bounced_unreserve_in.queue, self.on_bounced_unreserve_in)
        await self.channel.basic_consume(self.unreserve_done_in.queue, self.on_unreserve_done_in)



    @BouncedReserveMessage.unwrapped_message
    async def on_bounced_reserve_in(self, bounced_reserve: BouncedReserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Reserve {str(message.body.decode())}")
        reservation = await create_reservation_from_bounced_reserve(bounced_reserve)

        try:
            topic = await find_topic_for_reservation(bounced_reserve, reservation)
            if topic: 
                logger.warn(f"Reservation done: channel {topic}")
                # We are routing it to the Template channel (pods will pick up and then reply to)
                #reserve_done =  ReserveDoneMessage(data={"topic": topic}, meta={"reference": bounced_reserve.meta.reference})

                #TODO: Make this deciscion only if the on_reserve hook is overwritten
                reserve_progress =  ReserveProgressMessage(data={"level": ProgressLevel.INFO, "message": f"Using Topic {str(topic)}"}, meta={"reference": bounced_reserve.meta.reference})
                logger.info(reserve_progress)

                await self.forward(reserve_progress, bounced_reserve.meta.extensions.progress)
                await self.forward(bounced_reserve, topic)

            else:
                logger.info(f"Seeing if autoprovide works for {bounced_reserve.meta.token}")

                template = await find_providable_template_for_reservation(bounced_reserve, reservation)

                bounced_provide = await create_bouncedprovide_from_bouncedreserve_and_template(bounced_reserve, template, reservation)
                logger.info(bounced_provide)
                provide_progress =  ReserveProgressMessage(data={"level": ProgressLevel.INFO, "message": f"Providing Template {str(template.id)} on {template.provider}"}, meta={"reference": bounced_reserve.meta.reference})
                logger.info(provide_progress)

                await self.forward(provide_progress, bounced_reserve.meta.extensions.progress)
                await self.forward(bounced_provide, f"provision_in_{template.provider.unique}")
                
               
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, bounced_reserve.meta.reference)
            await self.forward(exception, bounced_reserve.meta.extensions.callback)
            console.print_exception()

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @BouncedUnreserveMessage.unwrapped_message
    async def on_bounced_unreserve_in(self, bounced_unreserve: BouncedUnreserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Unreserve {str(message.body.decode())}")

        try:
            topic = await find_topic_for_unreservation(bounced_unreserve)
            await self.forward(bounced_unreserve, str(topic))
                           
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, bounced_unreserve.meta.reference)
            await self.forward(exception, bounced_unreserve.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @UnreserveDoneMessage.unwrapped_message
    async def on_unreserve_done_in(self, unreserve_done: UnreserveDoneMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Unreserve {str(message.body.decode())}")

        try:
            await self.forward(unreserve_done, unreserve_done.meta.extensions.callback)
            

            unprovision, provider = await get_unprovision_to_cause(unreserve_done)

            logger.info(f'Finally we can unprovide what we provided before')
            if unprovision is not None:
                await self.forward(unprovision, f"unprovision_in_{provider.unique}")


        except Exception as e:
            logger.error(e)

        await message.channel.basic_ack(message.delivery.delivery_tag)




