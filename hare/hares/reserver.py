
from facade.subscriptions.provision import MyProvisionsEvent, ProvisionEventSubscription
from delt.messages.generics import Token
from delt.messages.postman.reserve.params import ReserveParams
from facade.consumers.postman import get_topic_for_bounced_assign
from facade.utils import create_assignation_from_bounced_assign, log_to_assignation, log_to_provision, log_to_reservation, set_assignation_status, set_provision_status, set_reservation_status
from delt.messages.postman.provide.params import ProvideParams
from facade.enums import AssignationStatus, LogLevel, PodStatus, ProvisionStatus, ReservationStatus, TopicStatus
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages import *
from typing import Tuple, Union
from facade.models import Assignation, Provision, Reservation, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare
from arkitekt.console import console
logger = logging.getLogger(__name__)

class ProtocolException(Exception):
    pass

class ReserveError(ProtocolException):
    pass

class DeniedError(ProtocolException):
    pass

class NoTemplateFoundError(ReserveError):
    pass

class NotInReserveModeError(ReserveError):
    pass

class TemplateNotProvided(ReserveError):
    pass




def find_topic_for_bounced_reserve(bounced_reserve: BouncedReserveMessage) -> Tuple[str, MessageModel]:
    """Finds a topic for a Reservation

    Searches the database for active Topics that the user can reserve (the policy is informed by the template or specified for each topic individually for overarching policys (e.g. User A cannot
    assign to Templates of Provider A. 

    Args:
        bounced_reserve (BouncedReserveMessage): The bounced Reservation
    Returns:
        Pod: The Topics
    """

    reference = bounced_reserve.meta.reference
    callback = bounced_reserve.meta.extensions.callback

    reservation = Reservation.objects.get(reference=reference)

    params = ReserveParams(**reservation.params)
    context = Token(**reservation.context)
    node = reservation.node
    template = reservation.template


    if node and not template:
        # The Node path is more generic, we are just filteing by the node and the params
        qs = Template.objects.select_related("provider").filter(node=node)

        if params.providers:
            qs.filter(provider__pk__in=params.providers)

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods
        template =  qs.first()
        
    if template is None: raise NoTemplateFoundError(f"Did not find an Template for this Node {node} with params {params}")

    # Filter based on Assignment Policy and also on activity if deamnded
    provision = template.provisions.exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED]).first()

    # Here we check if we can assign to the Topic
    if provision is None: raise TemplateNotProvided("Template was not provided!")


    if provision.status == ProvisionStatus.ACTIVE:
        # We can just forward this to the provider
        log_message = "App is active and we can assign to the Provision. Forwarding to App"
        log_to_reservation(reservation.reference, log_message, level=LogLevel.INFO)
        return f"reservations_in_{provision.unique}", bounced_reserve
        
    else:
        log_message = "Attention the App is inactive, please start the App. We are waiting for that! (Message stored in Database)"
        log_to_reservation(reservation.reference, log_message, level=LogLevel.WARN)
        set_reservation_status(reservation.reference, ReservationStatus.WAITING)
        message = ReserveWaitingMessage(data={
            "provision": provision.id,
            "message": log_message
        }, meta={
            "reference": reference
        })
        return callback, message



def provide_topic_for_reservation(bounced_reserve: BouncedReserveMessage) -> Tuple[str, MessageModel]:


    reference = bounced_reserve.meta.reference
    callback = bounced_reserve.meta.extensions.callback

    reservation = Reservation.objects.get(reference=reference)

    context = Token(**reservation.context) #TODO: Inadaptly name
    params = ReserveParams(**reservation.params)

    
    if "can_provide" not in context.scopes: raise DeniedError("Your App does not have the proper permissions set to autoprovide 'can_provide' to your scopes")

    node = reservation.node
    template = reservation.template

    if node and not template:
        qs = Template.objects.select_related("provider").filter(node=node)

        if params.providers:
            qs.filter(provider__pk__in=params.providers)
        
        # TODO: Check if this is okay

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods

        template =  qs.first()
        
        
    if template is None: NoTemplateFoundError(f"Did not find a providable Template for this Node {node} with params {params}. No App implementing this Node with this Params was active.")

    reservation.node = template.node
    reservation.save()

    # Why not make topic and Provision the same?
    # IDea: Provision is an idefinete log and an action, topic is the actual state 
    provision = Provision.objects.create(
        reservation = reservation,
        status= ProvisionStatus.PROVIDING,
        reference= reservation.reference, # We use the same reference that the user wants
        context= reservation.context, # We use the same reference that the user wants
        template= template,
        params = reservation.params,
        extensions = reservation.extensions,
        app = reservation.app,
        creator = reservation.creator,
    )

    # We add the creating topic to the reservation
    provision.reservations.add(reservation)
    provision.save()

    # Signal Broadcasting to Channel Layer for Persistens
    MyProvisionsEvent.broadcast({"action": "created", "data": provision.id}, [f"provisions_user_{context.user}"])
    ProvisionEventSubscription.broadcast({"action": "updated", "data": reservation.id}, [f"provision_{reservation.reference}"])

    if template.provider.active:
        log_to_reservation(reservation.reference, "Provider is active we are sending it to the Provider", level=LogLevel.INFO)
        set_reservation_status(reservation.reference, ReservationStatus.PROVIDING)

        provide_message = BouncedProvideMessage(data= {
                    "template": provision.template.id,
                    "params": provision.params,
                },
                meta= {
                    "reference": provision.reference,
                    "extensions": provision.extensions,
                    "token": provision.context
        })
    
        return f"provision_in_{provision.template.provider.unique}", provide_message

    else:
        log_message = f"App {template.provider.name} is currently unactive. We will get notified once it gets connected"
        log_to_reservation(reservation.reference, log_message, level=LogLevel.INFO)
        set_reservation_status(reservation.reference, ReservationStatus.WAITING)
        message = ReserveWaitingMessage(data={
            "prosvision": provision.id,
            "message": log_message
        }, meta={
            "reference": reference
        })
        return callback, message
         


def prepare_messages_for_unreservation(bounced_unreserve: BouncedUnreserveMessage):
    """Checks if the Topic is still active

    Args:
        bounced_unreserve (BouncedUnreserveMessage): [description]

    Raises:
        e: [description]

    """
    context = bounced_unreserve.meta.token
    res = Reservation.objects.get(reference=bounced_unreserve.data.reservation)
    if context.user != res.creator_id and "admin" not in context.roles: raise DeniedError("Only the user that created the Reservation or an admin can unreserve his pods") 
    set_reservation_status(res.reference, ReservationStatus.CANCELING)

    messages= []
    for provision in res.provisions.all():
        if provision.status == ProvisionStatus.ACTIVE:
            log_to_reservation(res.reference, f"Sending Unreservation to Topic {provision.unique}")
            channel = f"reservations_in_{provision.unique}"
            messages.append((channel, bounced_unreserve))
        else:
            log_to_reservation(res.reference, f"Topic {provision.unique} is not currently active. Unreserving")
            provision.reservations.remove(res)

    return messages


def prepare_messages_for_assignment(bounced_assign: BouncedAssignMessage):
    """Preperaes messages for unprovision

    Args:
        bounced_unreserve (BouncedUnprovideMessage): [description]

    Raises:
        e: [description]

    """
    reservation = bounced_assign.data.reservation
    reference = bounced_assign.meta.reference
    context = bounced_assign.meta.token
    res = Reservation.objects.get(reference=reservation)
    if context.user != res.creator_id and "admin" not in context.roles: raise DeniedError("Only the user that created the Reservation or an admin can assign to it") 
    
    messages= []
    if res.status == ReservationStatus.ACTIVE:
        set_assignation_status(reference, AssignationStatus.ASSIGNED)
        messages.append((f"assignments_in_{res.channel}", bounced_assign))

    else:
        set_assignation_status(reference, AssignationStatus.DENIED)

    return messages






def prepare_messages_for_unprovision(bounced_unprovide: BouncedUnprovideMessage):
    """Preperaes messages for unprovision

    Args:
        bounced_unreserve (BouncedUnprovideMessage): [description]

    Raises:
        e: [description]

    """
    provision_reference = bounced_unprovide.data.provision
    reference = bounced_unprovide.meta.reference
    context = bounced_unprovide.meta.token
    prov = Provision.objects.get(reference=provision_reference)
    if context.user != prov.creator_id and "admin" not in context.roles: raise DeniedError("Only the user that created the Reservation or an admin can unreserve his pods") 
    
    messages= []
    if prov.status == ProvisionStatus.ACTIVE:
        set_provision_status(prov.reference, ProvisionStatus.CANCELING)
        forwarded_message = BouncedUnprovideMessage(data={"provision": provision_reference}, meta={"reference": reference, "token": context})
        messages.append((f"provision_in_{prov.template.provider.unique}", forwarded_message))

    else:
        set_provision_status(prov.reference, ProvisionStatus.CANCELLED)
        if bounced_unprovide.meta.extensions.callback:
            unprovide_done_message = UnprovideDoneMessage(data={"provision": provision_reference}, meta={"reference": reference, "token": context})
            messages.append((bounced_unprovide.meta.extensions.callback, unprovide_done_message))

    return messages



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
        self.bounced_unprovide_in = await self.channel.queue_declare("bounced_unprovide_in")
        self.unreserve_done_in = await self.channel.queue_declare("unreserve_done_in")

        self.bounced_assign_in = await self.channel.queue_declare("bounced_assign_in")
        self.bounced_unassign_in = await self.channel.queue_declare("bounced_unassign_in")


        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.bounced_reserve_in.queue, self.on_bounced_reserve_in)
        await self.channel.basic_consume(self.bounced_unreserve_in.queue, self.on_bounced_unreserve_in)
        await self.channel.basic_consume(self.bounced_unprovide_in.queue, self.on_bounced_unprovide_in)
        await self.channel.basic_consume(self.unreserve_done_in.queue, self.on_unreserve_done_in)
        await self.channel.basic_consume(self.bounced_assign_in.queue, self.on_bounced_assign_in)
        await self.channel.basic_consume(self.bounced_unassign_in.queue, self.on_bounced_unassign_in)



    async def log_to_reservation(self, reference, message, level = LogLevel.INFO, logCallback=None):
        if logCallback is not None:
            reserve_progress =  ReserveLogMessage(data={"level": level, "message": message}, meta={"reference": reference})
            logger.info(reserve_progress)

            await self.forward(reserve_progress, logCallback)

        await sync_to_async(log_to_reservation)(reference, message, level=level)

    async def log_to_provision(self, reference, message, level = LogLevel.INFO, logCallback=None):
        if logCallback is not None:
            reserve_progress =  ProvideLogMessage(data={"level": level, "message": message}, meta={"reference": reference})
            logger.info(reserve_progress)

            await self.forward(reserve_progress, logCallback)

        await sync_to_async(log_to_provision)(reference, message, level=level)


    async def log_to_assignation(self, reference, message, level = LogLevel.INFO):
        await log_to_assignation(reference, message, level=level)


    async def set_reservation_status(self, reference, status):
        await sync_to_async(set_reservation_status)(reference, status)
        logger.info(reference)


    @BouncedReserveMessage.unwrapped_message
    async def on_bounced_reserve_in(self, bounced_reserve: BouncedReserveMessage, aiomessage: aiormq.abc.DeliveredMessage):
        """Bounced Reserve In

        Bounced reserve will only be called once a Reservation has already been called with a reference to the created
        reservation

        Args:
            bounced_reserve (BouncedReserveMessage): [description]
            message (aiormq.abc.DeliveredMessage): [description]

        """
        try:

            logger.info(f"Received Bounced Reserve {str(aiomessage.body.decode())} {bounced_reserve}")

            reference = bounced_reserve.meta.reference
            params = bounced_reserve.data.params
            context = bounced_reserve.meta.token
            logCallback = bounced_reserve.meta.extensions.progress
            
            
            # Callback
            callback = bounced_reserve.meta.extensions.callback

            await self.log_to_reservation(reference, f"Reserver Received Event", level=LogLevel.INFO, logCallback=logCallback)

            try:

                try:
                    channel, message = await sync_to_async(find_topic_for_bounced_reserve)(bounced_reserve)
                    await self.forward(message, channel)

                except TemplateNotProvided as e:
                    if params.auto_provide != True: raise e
                    
                    channel, message = await sync_to_async(provide_topic_for_reservation)(bounced_reserve)
                    await self.forward(message, channel)


            except ProtocolException as e:
                logger.exception(e)
                exception = ExceptionMessage.fromException(e, reference)
                await self.set_reservation_status(reference, ReservationStatus.ERROR)
                await self.log_to_reservation(reference, f"{str(e)}", level=LogLevel.ERROR, logCallback=logCallback )
                await self.forward(exception, callback)


            
            # This should then expand this to an assignation message that can be delivered to the Providers
            await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)
    
               
        except Exception as e:
            logger.exception(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.set_reservation_status(reference, ReservationStatus.CRITICAL)
            await self.log_to_reservation(reference, f"Protocol Exception {str(e)}", level=LogLevel.ERROR, logCallback=logCallback )
            await self.forward(exception, callback)



    @BouncedUnreserveMessage.unwrapped_message
    async def on_bounced_unreserve_in(self, bounced_unreserve: BouncedUnreserveMessage, aiomessage: aiormq.abc.DeliveredMessage):
        try:

            logger.info(f"Received Bounced Reserve {str(aiomessage.body.decode())} {bounced_reserve}")

            reference = bounced_unreserve.meta.reference
            callback = bounced_unreserve.meta.extensions.callback
            reservation = bounced_unreserve.data.reservation
            context = bounced_unreserve.meta.token

            await self.log_to_reservation(reservation, f"Unreserve Request received", level=LogLevel.INFO)

            try:
                messages = await sync_to_async(prepare_messages_for_unreservation)(bounced_unreserve)

                if len(messages) > 0:
                    for channel, message in messages: 
                        await self.forward(message, channel)

                else:
                    await sync_to_async(log_to_reservation)(reservation, f"No active Topics need cancellation. We can shutdown", level=LogLevel.INFO)
                    await sync_to_async(set_reservation_status)(reservation, ReservationStatus.CANCELLED)
                    
                    unreserve_done = UnreserveDoneMessage(data={
                        "reservation": bounced_unreserve.data.reservation
                    },
                    meta = {
                        "reference": bounced_unreserve.meta.reference,
                        "extensions": bounced_unreserve.meta.extensions,
                    }
                    )
                    await self.forward(unreserve_done, callback)
            
            except ProtocolException as e:
                logger.exception(e)
                exception = ExceptionMessage.fromException(e, reference)
                await self.set_reservation_status(reference, ReservationStatus.ERROR)
                await self.log_to_reservation(reference, f"Unreservation Error: {str(e)}", level=LogLevel.ERROR)
                await self.forward(exception, callback)


            # This should then expand this to an assignation message that can be delivered to the Providers
            await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

                         
        except Exception as e:
            logger.exception(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.set_reservation_status(reference, ReservationStatus.CRITICAL)
            await self.log_to_reservation(reservation, f"Protocol Error on Unreservation", level=LogLevel.ERROR)
            await self.forward(exception, callback)



    @BouncedUnprovideMessage.unwrapped_message
    async def on_bounced_unprovide_in(self, bounced_unreserve: BouncedUnprovideMessage, aiomessage: aiormq.abc.DeliveredMessage):
        try:

            logger.info(f"Received Bounced Unprovide {str(aiomessage.body.decode())} {bounced_unreserve}")

            reference = bounced_unreserve.meta.reference
            callback = bounced_unreserve.meta.extensions.callback
            provision = bounced_unreserve.data.provision
            context = bounced_unreserve.meta.token

            await self.log_to_provision(provision, f"Unprovide Request received", level=LogLevel.INFO)

            try:
                messages = await sync_to_async(prepare_messages_for_unprovision)(bounced_unreserve)

                for channel, message in messages: 
                    logger.info(f"Sending {message} to {channel}")
                    await self.forward(message, channel)

            
            except ProtocolException as e:
                logger.exception(e)
                exception = ExceptionMessage.fromException(e, reference)
                await sync_to_async(set_provision_status)(reference, ProvisionStatus.ERROR)
                await sync_to_async(log_to_provision)(reference, f"Unreservation Error: {str(e)}", level=LogLevel.ERROR)
                if callback: await self.forward(exception, callback)


            # This should then expand this to an assignation message that can be delivered to the Providers
            await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

                         
        except Exception as e:
            logger.exception(e)
            exception = ExceptionMessage.fromException(e, reference)
            await sync_to_async(set_provision_status)(reference, ReservationStatus.CRITICAL)
            await sync_to_async(log_to_provision)(f"Protocol Error on Unreservation", level=LogLevel.ERROR)
            if callback: await self.forward(exception, callback)


    @UnreserveDoneMessage.unwrapped_message
    async def on_unreserve_done_in(self, unreserve_done: UnreserveDoneMessage, message: aiormq.abc.DeliveredMessage):
        logger.info(f"Received Bounced Unreserve Done {str(message.body.decode())}")

        reservation = unreserve_done.data.reservation
        reference = unreserve_done.meta.reference
        callback = unreserve_done.meta.extensions.callback
    
        await self.log_to_reservation(reservation, f"Unreservation was acknowledged by Pod!", level=LogLevel.INFO)

        try:
            await self.forward(unreserve_done, callback)
            
            unprovision, provider = await get_unprovision_to_cause(unreserve_done)

            logger.info(f'Finally we can unprovide what we provided before')
            if unprovision is not None:
                await self.log_to_reservation(reservation, f"We can cause an Unprovision for this pod.. Causing Unprovide (not waiting for it)", level=LogLevel.INFO)
                await self.forward(unprovision, f"unprovision_in_{provider.unique}") #TODO: Should we do this before deleting the reservation?
            
            
            await end_reservation(unreserve_done.data.reservation)
            await self.log_to_reservation(reservation, f"Sucessfully Unprovided this", level=LogLevel.INFO)

        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.log_to_reservation(reservation, f"Protocol Error on Unreservation.. Done Processment", level=LogLevel.ERROR)
            await self.forward(exception, callback)


        await message.channel.basic_ack(message.delivery.delivery_tag)

    @BouncedAssignMessage.unwrapped_message
    async def on_bounced_assign_in(self, bounced_assign: BouncedAssignMessage, aiomessage: aiormq.abc.DeliveredMessage):
        logger.info(f"Received Bounced Assign {str(aiomessage.body.decode())}")

        reference = bounced_assign.meta.reference
        callback = bounced_assign.meta.extensions.callback

        try:
            messages = await sync_to_async(prepare_messages_for_assignment)(bounced_assign)
            for channel, message in messages:
                logger.info(f"Assigning {message} to {channel}")
                await self.forward(message, channel)
                
               
        except Exception as e:
            logger.exception(e)
            exception = ExceptionMessage.fromException(e, bounced_assign.meta.reference)
            await sync_to_async(log_to_assignation)(reference, str(e), level=LogLevel.CRITICAL)
            await sync_to_async(set_assignation_status)(reference, AssignationStatus.CRITICAL.value)
            if callback: await self.forward(exception, bounced_assign.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)


    @BouncedUnassignMessage.unwrapped_message
    async def on_bounced_unassign_in(self, bounced_unassign: BouncedUnassignMessage, message: aiormq.abc.DeliveredMessage):
        logger.info(f"Received Bounced Unassign {str(message.body.decode())}")

        reference = bounced_unassign.meta.reference
        assignation = bounced_unassign.data.assignation
        callback =  bounced_unassign.meta.extensions.callback

        await self.log_to_assignation(assignation, f"Unreserve Request received", level=LogLevel.INFO)

        try:
            topic = await find_topic_for_unassignation(bounced_unassign)

            if topic: 
                # We first need to unprovide this Reservation
                await self.log_to_assignation(assignation, f"Sending unassignation to Pod", level=LogLevel.INFO)
                await self.forward(bounced_unassign, str(topic))
            else:
                
                await end_assignation(assignation)

                unreserve_done = UnassignDoneMessage(data={
                    "assignation": bounced_unassign.data.assignation
                },
                meta = {
                    "reference": bounced_unassign.meta.reference,
                    "extensions": bounced_unassign.meta.extensions,
                }
                )

                await self.log_to_assignation(assignation, f"Unassignation could not be delived to pod because pod is no longer active.. Probably it disconnected", level=LogLevel.ERROR)
                await self.forward(unreserve_done, callback)


                           
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.log_to_assignation(assignation, f"Protocol Error on Unassignation {str(e)}", level=LogLevel.ERROR)
            await self.forward(exception, callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)



