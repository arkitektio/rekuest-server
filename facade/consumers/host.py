# chat/consumers.py
import asyncio
from typing import List, Tuple
from facade.utils import log_to_assignation, log_to_provision, log_to_reservation, set_assignation_status, set_provision_status, set_reservation_status

from delt.messages.types import BOUNCED_FORWARDED_ASSIGN, BOUNCED_FORWARDED_RESERVE, RESERVE_DONE, UNRESERVE_DONE
import json
from delt.messages import *
from herre.bouncer.utils import bounced_ws
from ..models import Reservation, Provision
from ..enums import AssignationStatus, LogLevel, PodStatus, ProvisionStatus, ReservationStatus
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq
from delt.messages.utils import expandFromRabbitMessage
from arkitekt.console import console

logger = logging.getLogger(__name__)


def activateProvision(provision_reference) -> Tuple[str, List[str],List[Tuple[str, MessageModel]]]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """
    provision = Provision.objects.get(reference=provision_reference)
    set_provision_status(provision.reference, ProvisionStatus.ACTIVE)


    messages = []
    channels = []
    for res in provision.reservations.all():
        console.log(f"[green] Listening to {res}")
        log_to_reservation(res.reference, f"Listening now to {provision.unique}", level=LogLevel.INFO)
        set_reservation_status(res.reference, ReservationStatus.ACTIVE)
        
        message = ReserveActiveMessage(data={
            "provision": provision.id
            },
            meta={
                "reference": res.reference,
            }
        )

        messages.append((res.callback, message))
        channels.append(f"assignments_in_{res.channel}")

    return f"reservations_in_{provision.unique}", channels, messages


def cancelProvision(provision_reference) -> Tuple[List[str],List[Tuple[str, MessageModel]]]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """
    provision = Provision.objects.get(reference=provision_reference)
    set_provision_status(provision.reference, ProvisionStatus.CANCELLED)

    messages = []
    for res in provision.reservations.all():
        console.log(f"[yellow] Disconnecting Reservation {res}")
        log_to_reservation(res.reference, f"Provision cancelled shutdown {provision.unique}", level=LogLevel.WARN)
        set_reservation_status(res.reference, ReservationStatus.REROUTING)
        
        message = ReserveCriticalMessage(data={
            "message": f"Provision cancelled shutdown {provision.unique}"
            },
            meta={
                "reference": res.reference,
            }
        )
        messages.append((res.callback, message))
        provision.reservations.remove(res)

    return messages

def killProvision(provision_reference) -> Tuple[List[str],List[Tuple[str, MessageModel]]]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """
    provision = Provision.objects.get(reference=provision_reference)
    set_provision_status(provision.reference, ProvisionStatus.DISCONNECTED)

    messages = []
    for res in provision.reservations.all():
        console.log(f"[yellow] Disconnecting Reservation {res}")
        log_to_reservation(res.reference, f"Lost Connection to {provision.unique}", level=LogLevel.WARN)
        set_reservation_status(res.reference, ReservationStatus.ERROR)
        
        message = ReserveCriticalMessage(data={
            "message": f"Lost Connection to {provision.unique}"
            },
            meta={
                "reference": res.reference,
            }
        )
        messages.append((res.callback, message))
        # provision.reservations.remove(res) TODO: Unlink only if we can link anew

    

    return messages


def addReservationToProvision(provision_reference, reservation_reference) -> Tuple[str,str, MessageModel]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """
    provision = Provision.objects.get(reference=provision_reference)
    reservation = Reservation.objects.get(reference=reservation_reference)

    assert provision.status == ProvisionStatus.ACTIVE, "Very weird error"

    provision.reservations.add(reservation)
    provision.save()

    console.log(f"[green] Listening to {reservation}")
    log_to_reservation(reservation.reference, f"Listening now to {provision.unique}", level=LogLevel.INFO)
    set_reservation_status(reservation.reference, ReservationStatus.ACTIVE)
        
    message = ReserveActiveMessage(data={
        "provision": provision.id
        },
        meta={
            "reference": reservation.reference,
        }
    )

    return f"assignments_in_{reservation.channel}", reservation.callback, message


def deleteReservationFromProvision(provision_reference, reservation_reference) -> Tuple[str,str, MessageModel]:
    """ Activatets the provision and sets the created topic
    to active as well as creating signal for every connecting Reservation that this Topic
    is now active. (This Reservations should have been previously waiting)

    Args:
        reference ([type]): [description]
    """
    provision = Provision.objects.get(reference=provision_reference)
    reservation = Reservation.objects.get(reference=reservation_reference)
    assert provision.status == ProvisionStatus.ACTIVE, "Very weird error"

    provision.reservations.remove(reservation)
    provision.save()

    console.log(f"[yellow] Removing {reservation}")
    log_to_reservation(reservation.reference, f"Stopped Listening to {provision.unique}", level=LogLevel.INFO)

    # If all topic links for this reservation are set we can just say bye bye
    if reservation.provisions.count() == 0:
        set_reservation_status(reservation.reference, ReservationStatus.CANCELLED)
        return True

    return False





class HostConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {

        AssignYieldsMessage: lambda cls: cls.on_assign_yields,
        AssignLogMessage: lambda cls: cls.on_assign_log,
        AssignCriticalMessage: lambda cls: cls.on_assign_critical,
        AssignReturnMessage: lambda cls: cls.on_assign_return,
        AssignDoneMessage: lambda cls: cls.on_assign_done,
        AssignCancelledMessage: lambda cls: cls.on_assign_cancelled,


        UnassignCriticalMessage: lambda cls: cls.dummy_test,
        UnassignDoneMessage: lambda cls: cls.dummy_test,
        UnassignLogMessage: lambda cls: cls.dummy_test,

        ProvideDoneMessage: lambda cls: cls.on_provide_done,

        UnprovideDoneMessage: lambda cls: cls.on_unprovide_done,
        UnprovideLogMessage: lambda cls: cls.on_unprovide_log,
        UnprovideCriticalMessage: lambda cls: cls.on_unprovide_critical,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        logger.warning(f'Connecting Host {self.scope["bounced"].app.name}') 

        self.provision_queues = {}
        self.hosted_topics = {}
        self.channel_name = await self.connect_to_rabbit()

        self.provision_link_map = {}

    async def dummy_test(self, message):
        print(message)


    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        
    async def on_assign_related(self, provision_reference, message: aiormq.abc.DeliveredMessage):
        logger.error("Received somethign here")
        nana = expandFromRabbitMessage(message)
        
        if isinstance(nana, BouncedAssignMessage):
            forwarded_message = BouncedForwardedAssignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_ASSIGN})
            await self.send_message(forwarded_message) # No need to go through pydantic???
            
        elif isinstance(nana, BouncedUnassignMessage):
            await self.send_message(nana) 
        
        else:
            logger.error("This message is not what we expeceted here")
            
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_reservation_related(self, provision_reference, aiomessage: aiormq.abc.DeliveredMessage):
        message = expandFromRabbitMessage(aiomessage)
        
        if isinstance(message, BouncedReserveMessage):
            reservation_reference = message.meta.reference
            channel, message_callback, res_active_message = await sync_to_async(addReservationToProvision)(provision_reference, reservation_reference)
            # Set up connectiongs
            assert provision_reference in self.provision_link_map, "Topic is not provided"
            assign_queue = await self.channel.queue_declare(channel)
            await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(provision_reference, x))
            self.provision_link_map[provision_reference].append(assign_queue)
            await self.forward(res_active_message, message_callback)

        elif isinstance(message, BouncedUnreserveMessage):
             reservation_reference = message.data.reservation
             all_done = await sync_to_async(deleteReservationFromProvision)(provision_reference, reservation_reference)
             if all_done:
                done_message = UnreserveDoneMessage(data=message.data, meta={"reference": message.meta.reference})
                if message.meta.extensions.callback: await self.forward(done_message, message.meta.extensions.callback)
        
        else:
            logger.exception(Exception("This message is not what we expeceted here"))
            
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting Host with close_code {close_code}") 
            #TODO: Depending on close code send message to all running Assignations
            # We are assuming that every shutdown was ungently and check if we need to deactivate the pods
            await asyncio.gather(*[sync_to_async(killProvision)(prov) for prov, queues in self.provision_link_map.items()])

            #await asyncio.gather(*[self.destroy_pod(id) for id, queue in self.hosted_topics.items()])

            await self.connection.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")



    async def on_assign_critical(self, assign_critical: AssignCriticalMessage):
        await sync_to_async(log_to_assignation)(assign_critical.meta.reference, assign_critical.data.type + " : " + assign_critical.data.message, level=LogLevel.CRITICAL)
        await sync_to_async(set_assignation_status)(assign_critical.meta.reference, AssignationStatus.CRITICAL)

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        await sync_to_async(log_to_assignation)(assign_yield.meta.reference, f"Yielded {assign_yield.data.returns}", level=LogLevel.INFO)
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_cancelled(self, assign_cancelled: AssignCancelledMessage):
        await sync_to_async(log_to_assignation)(assign_cancelled.meta.reference, f"Cancelled on Consumer Side", level=LogLevel.CRITICAL)
        await self.forward(assign_cancelled, assign_cancelled.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        await sync_to_async(log_to_assignation)(assign_return.meta.reference, f"Returned {assign_return.data.returns}", level=LogLevel.INFO)
        await sync_to_async(set_assignation_status)(assign_return.meta.reference, AssignationStatus.DONE)
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_done(self, assign_done: AssignDoneMessage):
        await sync_to_async(log_to_assignation)(assign_done.meta.reference, f"Assignation Done", level=LogLevel.INFO)
        await sync_to_async(set_assignation_status)(assign_done.meta.reference, AssignationStatus.DONE)
        await self.forward(assign_done, assign_done.meta.extensions.callback)

    async def on_assign_log(self, assign_log: AssignLogMessage):
        await sync_to_async(log_to_assignation)(assign_log.meta.reference, assign_log.data.message, level=assign_log.data.level)
        await self.forward(assign_log, assign_log.meta.extensions.progress)

    async def on_provide_done(self, provide_done: ProvideDoneMessage):
        logger.error("Done here")
        reference = provide_done.meta.reference
        self.provision_link_map[reference] = []

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

        logger.info(f"Providing Done {provide_done}") 

    async def on_unprovide_done(self, unprovide_done: UnprovideDoneMessage):
        reference = unprovide_done.data.provision
        self.provision_link_map[reference] = []

        messages = await sync_to_async(cancelProvision)(reference)

        del self.provision_link_map[reference]

        for channel, message in messages:
            if channel: await self.forward(message, channel)

        if unprovide_done.meta.extensions.callback : await self.forward(unprovide_done, unprovide_done.meta.extensions.callback)
        logger.info(f"Unproviding Done {unprovide_done}")  


    async def on_unprovide_log(self, unprovide_log: UnprovideLogMessage):
        reference = unprovide_log.data.provision
        await sync_to_async(log_to_provision)(reference, unprovide_log.data.message, unprovide_log.data.level)

    async def on_unprovide_critical(self, unprovide_critical: UnprovideCriticalMessage):
        reference = unprovide_critical.data.provision
        await sync_to_async(log_to_provision)(reference, unprovide_critical.data.message, level=LogLevel.CRITICAL)

    async def on_provide_critical(self, provide_critical: ProvideCriticalMessage):
        console.log(f"Providing Done {provide_critical}", style="blink")  

    async def on_unassign_done(self, assign_return: UnassignDoneMessage):
        print("oisdnoisndofinsdo9finh")
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_unassign_progress(self, assign_return: UnassignLogMessage):
        await self.forward(assign_return, assign_return.meta.extensions.progress)
    
    async def on_unassign_critical(self, assign_return: UnassignCriticalMessage):
        try:
            await log_to_assignation(assign_return.meta.reference, assign_return.data.message, level=LogLevel.ERROR)
        except Exception as e:
            logger.error(str(e))
        await self.forward(assign_return, assign_return.meta.extensions.callback)





        

