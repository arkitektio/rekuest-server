

from hare.transitions.assignation import cancel_assignation_by_reference, done_assignation_by_reference, return_assignation_by_reference, yield_assignation_by_reference
from delt.messages.postman.assign.assign_cancelled import AssignCancelledMessage
from delt.messages.postman.assign.assign_done import AssignDoneMessage
from delt.messages.postman.assign.assign_return import AssignReturnMessage
from delt.messages.postman.assign.assign_critical import AssignCriticalMessage
from delt.messages.postman.assign.assign_log import AssignLogMessage
from delt.messages.postman.assign.assign_yield import AssignYieldsMessage
from hare.transitions.base import TransitionException
from django.core.checks import messages
from delt.messages.postman.reserve.reserve_transition import ReserveState
from delt.messages.postman.unprovide.unprovide_critical import UnprovideCriticalMessage
from delt.messages.postman.unprovide.unprovide_log import UnprovideLogMessage
from typing import List, Tuple
from delt.messages.postman.provide.provide_log import ProvideLogMessage
from delt.messages.types import BOUNCED_FORWARDED_ASSIGN, BOUNCED_FORWARDED_UNASSIGN
from delt.messages.postman.unassign.bounced_unassign import BouncedUnassignMessage
from delt.messages.postman.assign.bounced_forwarded_assign import BouncedForwardedAssignMessage
from delt.messages.postman.unassign.bounced_forwarded_unassign import BouncedForwardedUnassignMessage
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.provide.provide_transition import ProvideState, ProvideTransistionData
from delt.messages import BouncedUnreserveMessage
from facade.utils import log_to_provision, transition_provision
from facade.consumers.base import BaseConsumer
from asgiref.sync import sync_to_async
from facade.helpers import create_assignation_from_bouncedassign, create_bounced_assign_from_assign, create_bounced_reserve_from_reserve, create_reservation_from_bouncedreserve, get_channel_for_reservation
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
import aiormq
from delt.messages.exception import ExceptionMessage
from delt.messages.base import MessageModel
from delt.messages import ProvideTransitionMessage, ProvideCriticalMessage, ReserveTransitionMessage
from channels.generic.websocket import AsyncWebsocketConsumer
from delt.messages.utils import expandFromRabbitMessage, expandToMessage, MessageError
import json
import logging
from lok.bouncer.utils import bounced_ws
import asyncio
from ..models import Provider, Provision, Reservation
from facade.subscriptions.provider import ProvidersEvent
from facade.subscriptions.reservation import MyReservationsEvent
from facade.subscriptions.provision import MyProvisionsEvent
from facade.enums import ProvisionStatus, ReservationStatus
from arkitekt.console import console
from hare.transitions.reservation import activate_reservation, disconnect_reservation, critical_reservation, cancel_reservation
from hare.transitions.provision import activate_provision, cancel_provision, cancelling_provision, disconnect_provision, critical_provision, providing_provision

logger = logging.getLogger(__name__)


def activate_and_add_reservation_to_provision(res: Reservation, prov: Provision):
    prov.reservations.add(res)
    prov.save() 
    return activate_reservation(res)

def cancel_and_delete_reservation_from_provision(res: Reservation, prov: Provision):
    prov.reservations.remove(res)
    prov.save() 
    return cancel_reservation(res)



def activate_and_add_reservation_to_provision_by_reference(res_reference: str, prov_reference:str):
    res = Reservation.objects.get(reference=res_reference)
    prov = Provision.objects.get(reference=prov_reference)
    return activate_and_add_reservation_to_provision(res, prov)

def cancel_and_delete_reservation_from_provision_by_reference(res_reference: str, prov_reference:str):
    res = Reservation.objects.get(reference=res_reference)
    prov = Provision.objects.get(reference=prov_reference)
    return cancel_and_delete_reservation_from_provision(res, prov)


def activate_provision_by_reference(reference):
    provision = Provision.objects.get(reference=reference)
    return activate_provision(provision)


def critical_provision_by_reference(reference):
    provision = Provision.objects.get(reference=reference)
    return critical_provision(provision)

def cancel_provision_by_reference(reference):
    provision = Provision.objects.get(reference=reference)
    return cancel_provision(provision)

def cancelling_provision_by_reference(reference):
    provision = Provision.objects.get(reference=reference)
    return cancelling_provision(provision)

def providing_provision_by_reference(reference):
    provision = Provision.objects.get(reference=reference)
    return providing_provision(provision)

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

    return provider, requests



def deactivate_provider_and_disconnect_active_provisions(provider, reconnect_provision = False):
    provider.active = False
    provider.save()

    provisions = Provision.objects.filter(template__provider=provider).exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED]).all()

    messages = []
    for provision in provisions:
        messages += disconnect_provision(provision, message="Disconnected trying to reconnect", reconnect=reconnect_provision)

    if provider.user: 
        ProvidersEvent.broadcast({"action": "ended", "data": provider.id}, [f"providers_user_{provider.user.id}"])
    else: 
        ProvidersEvent.broadcast({"action": "ended", "data": provider.id}, [f"all_providers"])

    return provider, messages



class AgentConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        ProvideTransitionMessage: lambda cls: cls.on_provide_transition,
        ProvideLogMessage: lambda cls: cls.on_provide_log,



        AssignYieldsMessage: lambda cls: cls.on_assign_yields,
        AssignLogMessage: lambda cls: cls.on_assign_log,
        AssignCriticalMessage: lambda cls: cls.on_assign_critical,
        AssignReturnMessage: lambda cls: cls.on_assign_return,
        AssignDoneMessage: lambda cls: cls.on_assign_done,
        AssignCancelledMessage: lambda cls: cls.on_assign_cancelled,

    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        #TODO: Check if in provider mode
        self.provider, start_provisions = await sync_to_async(activate_provider_and_get_active_provisions)(self.scope["bounced"].app, self.scope["bounced"].user)

        logger.warning(f"Connecting {self.provider.name}") 
        logger.info("This provide is now active and will be able to provide Pods")

        self.provision_link_map = {} # A link with all the queue indexed by provision
        self.assignments_tag_map = {}
        self.assignments_channel_map = {}

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
            logger.error(f"Something weird happened in disconnection! {e}")



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
        print("RECEIVED", aiomessage)
        
        try:
            if isinstance(message, BouncedReserveMessage):
                reservation_reference = message.meta.reference
                messages, assignment_topic = await sync_to_async(activate_and_add_reservation_to_provision_by_reference)(reservation_reference, provision_reference)
                # Set up connectiongs
                assert provision_reference in self.provision_link_map, "Provision is not provided"
                assign_queue = await self.channel.queue_declare(assignment_topic)

                await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(provision_reference, x))
                self.provision_link_map[provision_reference].append(assign_queue)
                
                for channel, message in messages:
                    await self.forward(message, channel)

            elif isinstance(message, BouncedUnreserveMessage):
                reservation_reference = message.data.reservation
                messages = await sync_to_async(cancel_and_delete_reservation_from_provision_by_reference)(reservation_reference, provision_reference)

                for channel, message in messages:
                    await self.forward(message, channel)
            
            else:
                raise Exception("This message is not what we expeceted here")

        except Exception as e:
            logger.exception(e)
            
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)


    async def on_assign_related(self, provision_reference, message: aiormq.abc.DeliveredMessage):
        nana = expandFromRabbitMessage(message)
        logger.info(message.delivery.delivery_tag)
        
        if isinstance(nana, BouncedAssignMessage):
            forwarded_message = BouncedForwardedAssignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_ASSIGN})
            await self.send_message(forwarded_message) # No need to go through pydantic???
            
        elif isinstance(nana, BouncedUnassignMessage):
            forwarded_message = BouncedForwardedUnassignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_UNASSIGN})
            await self.send_message(forwarded_message) 
        
        else:
            logger.error("This message is not what we expeceted here")
            
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_provide_transition(self, provide_transition: ProvideTransitionMessage):

        provision_reference = provide_transition.meta.reference
        new_state = provide_transition.data.state
        messages = []
        try:
            if new_state == ProvideState.ACTIVE:

                messages, reservation_topic, assignment_topics = await sync_to_async(activate_provision_by_reference)(provision_reference)

                logger.info(f"Listening to {reservation_topic}")
                reservation_queue = await self.channel.queue_declare(reservation_topic)
                await self.channel.basic_consume(reservation_queue.queue, lambda x: self.on_reservation_related(provision_reference, x))

                for channel in assignment_topics:
                    assign_queue = await self.channel.queue_declare(channel)
                    await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(provision_reference, x))
                    self.provision_link_map.setdefault(provision_reference, []).append(assign_queue)

            elif new_state == ProvideState.CRITICAL:
                messages = await sync_to_async(critical_provision_by_reference)(provision_reference)

            elif new_state == ProvideState.CANCELLED:
                messages = await sync_to_async(cancel_provision_by_reference)(provision_reference)

            elif new_state == ProvideState.CANCELING:
                messages = await sync_to_async(cancelling_provision_by_reference)(provision_reference)

            elif new_state == ProvideState.PROVIDING:
                messages = await sync_to_async(providing_provision_by_reference)(provision_reference)
            
            else: 
                raise NotImplementedError(f"No idea how to transition prov to {new_state}")


            for channel, message in messages:
                await self.forward(message, channel)

        except TransitionException as e:
            logger.error(e)

        except Exception as e:
            logger.exception(e)


    async def on_provide_log(self, message: ProvideLogMessage):
        pass

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        if assign_yield.meta.extensions.persist == True: await sync_to_async(yield_assignation_by_reference)(assign_yield.meta.reference, assign_yield.data.returns)
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_cancelled(self, assign_cancelled: AssignCancelledMessage):
        if assign_cancelled.meta.extensions.persist == True: await sync_to_async(cancel_assignation_by_reference)(assign_cancelled.meta.reference)
        await self.forward(assign_cancelled, assign_cancelled.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        if assign_return.meta.extensions.persist == True: await sync_to_async(return_assignation_by_reference)(assign_return.meta.reference, assign_return.data.returns)
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_done(self, assign_done: AssignDoneMessage):
        if assign_done.meta.extensions.persist == True: await sync_to_async(done_assignation_by_reference)(assign_done.meta.reference)
        await self.forward(assign_done, assign_done.meta.extensions.callback)

    async def on_assign_log(self, assign_log: AssignLogMessage):
        await self.forward(assign_log, assign_log.meta.extensions.progress)
        


        



