

from delt.messages.postman.provide.provide_log import ProvideLogMessage
from delt.messages.types import BOUNCED_FORWARDED_ASSIGN, BOUNCED_FORWARDED_UNASSIGN
from delt.messages.postman.unassign.bounced_unassign import BouncedUnassignMessage
from delt.messages.postman.assign.bounced_forwarded_assign import BouncedForwardedAssignMessage
from delt.messages.postman.unassign.bounced_forwarded_unassign import BouncedForwardedUnassignMessage
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.provide.provide_transition import ProvideState
from facade.utils import log_to_provision, transition_provision
from facade.consumers.base import BaseConsumer
from asgiref.sync import sync_to_async
from facade.consumers.postman import create_assignation_from_bouncedassign, create_bounced_assign_from_assign, create_bounced_reserve_from_reserve, create_reservation_from_bouncedreserve, get_channel_for_reservation
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
import aiormq
from delt.messages.exception import ExceptionMessage
from delt.messages.base import MessageModel
from delt.messages import ProvideTransitionMessage, ProvideCriticalMessage
from channels.generic.websocket import AsyncWebsocketConsumer
from delt.messages.utils import expandFromRabbitMessage, expandToMessage, MessageError
import json
import logging
from herre.bouncer.utils import bounced_ws
import asyncio
from ..models import Provider, Provision
from facade.subscriptions.provider import ProvidersEvent
from facade.enums import ProvisionStatus
from arkitekt.console import console

logger = logging.getLogger(__name__)



def activate_provider_and_get_active_provisions(app, user):

    if user is None or user.is_anonymous:
        provider = Provider.objects.get(app=app, user=None)
    else:
        provider = Provider.objects.get(app=app, user=user)

    provider.active = True
    provider.save()

    if provider.user: 
        ProvidersEvent.broadcast({"action": "started", "data": str(provider.id)}, [f"providers_user_{provider.user.id}"])
    else: 
        ProvidersEvent.broadcast({"action": "started", "data": provider.id}, [f"all_providers"])
    
    provisions = Provision.objects.filter(template__provider=provider).exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED]).all()

    requests = []
    for prov in provisions:
        requests.append(prov.to_message())

    print(requests)
    return provider, requests


def activate_provision_by_reference(reference: str):
    provision = Provision.objects.get(reference=reference)
    return active_provision(provision)

def transi(provision) -> Tuple[str, List[str],List[Tuple[str, MessageModel]]]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """


    messages = []
    channels = []
    for res in provision.reservations.all():
        console.log(f"[green] Listening to {res}")
        messages.append(log_to_reservation(res.reference, f"Listening now to {provision.unique}", level=LogLevel.INFO))
        messages += transition_reservation(res.reference, ReservationStatus.ACTIVE)
        channels.append(f"assignments_in_{res.channel}")

    return f"reservations_in_{provision.unique}", channels, messages



def deactivate_provider_and_disconnect_active_provisions(provider):
    provider.active = False
    provider.save()

    provisions = Provision.objects.filter(template__provider_id=provider.id).exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED]).all()

    messages = []
    for provision in provisions:
        messages += transition_provision(provision, ProvideState.DISCONNECTED, "Disconnected trying to reconnect")

    if provider.user: 
        ProvidersEvent.broadcast({"action": "ended", "data": provider.id}, [f"providers_user_{provider.user.id}"])
    else: 
        ProvidersEvent.broadcast({"action": "ended", "data": provider.id}, [f"all_providers"])

    return provider, messages



class AgentConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        ProvideTransitionMessage: lambda cls: cls.on_provide_transition,
        ProvideLogMessage: lambda cls: cls.on_provide_log

    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        #TODO: Check if in provider mode
        self.provider, start_provisions = await sync_to_async(activate_provider_and_get_active_provisions)(self.scope["bounced"].app, self.scope["bounced"].user)

        logger.warning(f"Connecting {self.provider.name}") 
        logger.info("This provide is now active and will be able to provide Pods")

        self.provision_link_map = {} # A link with all the queue indexed by provision

        await self.connect_to_rabbit()

        for prov in start_provisions:
            await self.send_message(prov)



    
    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting Provider {self.provider.name} with close_code {close_code}") 
            # We are deleting all associated Provisions for this Provider 
            provider, messages = await sync_to_async(deactivate_provider_and_disconnect_active_provisions)(self.provider)

            for channel, message in messages:
                await self.forward(message, channel)
            await self.connection.close()
        except Exception as e:
            logger.error("Something weird happened in disconnection! {e}")



    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # Declaring queue
        self.on_provide_queue = await self.channel.queue_declare(f"provision_in_{self.provider.unique}", auto_delete=True)

        await self.channel.basic_consume(self.on_provide_queue.queue, self.on_provide_related)


    async def on_provide_related(self, message: aiormq.abc.DeliveredMessage):
        """Provide Forward

        Simply forwards provide messages to the Provider on the Other end

        Args:
            message (aiormq.abc.DeliveredMessage): The delivdered message
        """
        await self.send(text_data=message.body.decode()) 
        # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def on_reservation_related(self, provision_reference, aiomessage: aiormq.abc.DeliveredMessage):
        message = expandFromRabbitMessage(aiomessage)
        
        if isinstance(message, BouncedReserveMessage):
            reservation_reference = message.meta.reference
            channel, messages = await sync_to_async(addReservationToProvision)(provision_reference, reservation_reference)
            # Set up connectiongs
            assert provision_reference in self.provision_link_map, "Topic is not provided"
            assign_queue = await self.channel.queue_declare(channel)

            await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(provision_reference, x))
            self.provision_link_map[provision_reference].append(assign_queue)
            
            for channel, message in messages:
                await self.forward(message, channel)

        elif isinstance(message, BouncedUnreserveMessage):
             reservation_reference = message.data.reservation
             messages = await sync_to_async(deleteReservationFromProvision)(provision_reference, reservation_reference)

             for channel, message in messages:
                await self.forward(message, channel)
        
        else:
            logger.exception(Exception("This message is not what we expeceted here"))
            
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)


    async def on_assign_related(self, provision_reference, message: aiormq.abc.DeliveredMessage):
        nana = expandFromRabbitMessage(message)
        
        if isinstance(nana, BouncedAssignMessage):
            forwarded_message = BouncedForwardedAssignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_ASSIGN})
            await self.send_message(forwarded_message) # No need to go through pydantic???
            
        elif isinstance(nana, BouncedUnassignMessage):
            forwarded_message = BouncedForwardedUnassignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_UNASSIGN})
            await self.send_message(nana) 
        
        else:
            logger.error("This message is not what we expeceted here")
            
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_provide_transition(self, provide_transition: ProvideTransitionMessage):

        messages = []

        if provide_transition.data.state == ProvideState.ACTIVE:
            self.provision_link_map[provide_transition.meta.reference] = []

            reservation_topic, new_channels, new_messages = await sync_to_async(activateProvision)(reference)

            reservation_queue = await self.channel.queue_declare(reservation_topic)
            await self.channel.basic_consume(reservation_queue.queue, lambda x: self.on_reservation_related(reference, x))

            for channel in new_channels:
                assign_queue = await self.channel.queue_declare(channel)
                await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(reference, x))
                self.provision_link_map[reference].append(assign_queue)

            if provide_done.meta.extensions.callback : await self.forward(provide_done, provide_done.meta.extensions.callback)

            



        for channel, message in new_messages:
                # Iterating over the Reservations
                if channel: await self.forward(message, channel)




    async def on_provide_log(self, message: ProvideLogMessage):
        await sync_to_async(log_to_provision)(message.meta.reference, message.data.message, level=message.data.level)
        if message.meta.extensions.progress is not None:
            await self.forward(message, message.meta.extensions.progress)

    async def on_unprovide_log(self, message: UnprovideLogMessage):
        await sync_to_async(log_to_provision)(message.data.provision, message.data.message, level=message.data.level)
        
    async def on_unprovide_critical(self, message: UnprovideCriticalMessage):
        await sync_to_async(log_to_provision)(message.data.provision, message.data.message, level=LogLevel.CRITICAL)
        


        



