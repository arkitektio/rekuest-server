
from facade.consumers.postman import get_topic_for_bounced_assign
from facade.utils import create_assignation_from_bounced_assign, create_reservation_from_bounced_reserve, end_assignation, end_reservation, log_to_assignation, log_to_reservation, set_assignation_status, set_reservation_status, update_reservation
from delt.messages.postman.provide.params import ProvideParams
from delt.messages.postman.progress import ProgressLevel
from facade.enums import AssignationStatus, LogLevel, PodStatus, ProvisionStatus, ReservationStatus
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages import *
from typing import Tuple, Union
from facade.models import Assignation, Pod, Provision, Reservation, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare
from arkitekt.console import console
logger = logging.getLogger(__name__)



@sync_to_async
def find_topic_for_reservation(reserve: BouncedReserveMessage, reservation: Reservation) -> str:
    params = reserve.data.params
    node = reserve.data.node
    template = reserve.data.template

    if node:
        qs = Template.objects.select_related("provider").filter(node_id=node)

        if params.providers:
            qs.filter(provider__pk__in=params.providers)

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods
        template =  qs.first()
        assert template is not None, f"Did not find an Template for this Node {reserve.data.node} with params {reserve.data.params}"


        pod = template.pods.filter(status=PodStatus.ACTIVE).first()

        if pod:
            update_reservation(reservation, template.node, template)
            pod.reservations.add(reservation)
            return pod.channel

        else:
            return None


@sync_to_async
def find_topic_for_unreservation(unreserve: BouncedUnreserveMessage) -> str:
    reservation = Reservation.objects.get(reference=unreserve.data.reservation)

    if reservation.pod:
        return reservation.pod.channel

    else:
        return None


@sync_to_async
def find_topic_for_unassignation(unreserve: BouncedUnassignMessage) -> str:
    assignation = Assignation.objects.get(reference=unreserve.data.assignation)

    if assignation.reservation:
        if assignation.reservation.pod:
            return assignation.reservation.pod.channel
        else:
            logger.info("Pod for reservation is already dead")
            return None


    else:
        logger.info("Assignation did never receive a Reservation")
        return None


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
        assert template.provider.active, "Provider of this template is not active!"
        update_reservation(reservation, template.node, template)
        return template

    if node:
        qs = Template.objects.select_related("provider").filter(node_id=node)

        if params.providers:
            qs.filter(provider__pk__in=params.providers)
        
        # TODO: Check if this is okay

        # Manage filtering by params {such and such} and by permissions to assign to some of the pods



        # Only Active Providers can Provide
        print(qs)
        qs = qs.filter(provider__active=True)
        print(qs)

        template =  qs.first()
        print(template)
        assert template is not None, f"Did not find a providable Template for this Node {reserve.data.node} with params {reserve.data.params}"
        update_reservation(reservation, template.node, template)
        return template

    raise Exception("Neither Node nor Template was specified. Crucial for Reservation!")



class ReserverRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.bounced_reserve_in = await self.channel.queue_declare("bounced_reserve_in")
        self.bounced_assign_in = await self.channel.queue_declare("bounced_assign_in")
        self.bounced_unassign_in = await self.channel.queue_declare("bounced_unassign_in")
        self.bounced_unreserve_in = await self.channel.queue_declare("bounced_unreserve_in")
        self.bounced_cancel_provide_in = await self.channel.queue_declare("bounced_cancel_provide_in")
        self.unreserve_done_in = await self.channel.queue_declare("unreserve_done_in")


        # We will get Results here
        self.provision_done = await self.channel.queue_declare("provision_done")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.bounced_reserve_in.queue, self.on_bounced_reserve_in)
        await self.channel.basic_consume(self.bounced_unreserve_in.queue, self.on_bounced_unreserve_in)
        await self.channel.basic_consume(self.unreserve_done_in.queue, self.on_unreserve_done_in)
        await self.channel.basic_consume(self.bounced_assign_in.queue, self.on_bounced_assign_in)
        await self.channel.basic_consume(self.bounced_unassign_in.queue, self.on_bounced_unassign_in)



    async def log_to_reservation(self, reference, message, level = ProgressLevel.INFO, logCallback=None):
        if logCallback is not None:
            reserve_progress =  ReserveProgressMessage(data={"level": level, "message": message}, meta={"reference": reference})
            logger.info(reserve_progress)

            await self.forward(reserve_progress, logCallback)

        await log_to_reservation(reference, message, level=level)


    async def log_to_assignation(self, reference, message, level = ProgressLevel.INFO):
        await log_to_assignation(reference, message, level=level)


    async def set_reservation_status(self, reference, status):
        await set_reservation_status(reference, status)
        logger.info(reference)




    @BouncedReserveMessage.unwrapped_message
    async def on_bounced_reserve_in(self, bounced_reserve: BouncedReserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Reserve {str(message.body.decode())} {bounced_reserve}")
        reservation = await create_reservation_from_bounced_reserve(bounced_reserve)

        reference = bounced_reserve.meta.reference
        logCallback = bounced_reserve.meta.extensions.progress
        callback = bounced_reserve.meta.extensions.callback

        await self.log_to_reservation(reference, f"Reserver Received Event", level=ProgressLevel.INFO, logCallback=logCallback)

        try:
            topic = await find_topic_for_reservation(bounced_reserve, reservation)
            if topic: 
                await self.log_to_reservation(reference, f"Using Topic {str(topic)}", level=ProgressLevel.INFO, logCallback=logCallback)
                await self.forward(bounced_reserve, topic)

            else:
                logger.info(f"Seeing if autoprovide works for {bounced_reserve.meta.token}")

                template = await find_providable_template_for_reservation(bounced_reserve, reservation)

                bounced_provide = await create_bouncedprovide_from_bouncedreserve_and_template(bounced_reserve, template, reservation)
                
                await self.set_reservation_status(reference, ReservationStatus.PROVIDING.value)
                await self.log_to_reservation(reference, f"Providing Template {str(template.id)} on {template.provider}", level=ProgressLevel.INFO, logCallback=logCallback )
                await self.forward(bounced_provide, f"provision_in_{template.provider.unique}")
                
               
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.set_reservation_status(reference, ReservationStatus.CRITICAL.value)
            await self.log_to_reservation(reference, f"Protocol Exception {str(e)}", level=ProgressLevel.ERROR, logCallback=logCallback )
            await self.forward(exception, callback)
            console.print_exception()

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @BouncedUnreserveMessage.unwrapped_message
    async def on_bounced_unreserve_in(self, bounced_unreserve: BouncedUnreserveMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Unreserve {str(message.body.decode())}")

        reference = bounced_unreserve.meta.reference
        reservation = bounced_unreserve.data.reservation
        callback =  bounced_unreserve.meta.extensions.callback

        await self.log_to_reservation(reservation, f"Unreserve Request received", level=ProgressLevel.INFO)

        try:
            topic = await find_topic_for_unreservation(bounced_unreserve)

            if topic: 
                # We first need to unprovide this Reservation
                await self.log_to_reservation(reservation, f"Sending unreservation to Pod", level=ProgressLevel.INFO)
                await self.forward(bounced_unreserve, str(topic))
            else:
                
                await end_reservation(bounced_unreserve.data.reservation)

                unreserve_done = UnreserveDoneMessage(data={
                    "reservation": bounced_unreserve.data.reservation
                },
                meta = {
                    "reference": bounced_unreserve.meta.reference,
                    "extensions": bounced_unreserve.meta.extensions,
                }
                )

                await self.log_to_reservation(reservation, f"Unreservation could not be delived to pod because pod is no longer active.. Probably it disconnected", level=ProgressLevel.ERROR)
                await self.forward(unreserve_done, callback)


                           
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.log_to_reservation(reservation, f"Protocol Error on Unreservation", level=ProgressLevel.ERROR)
            await self.forward(exception, callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @UnreserveDoneMessage.unwrapped_message
    async def on_unreserve_done_in(self, unreserve_done: UnreserveDoneMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Unreserve Done {str(message.body.decode())}")

        reservation = unreserve_done.data.reservation
        reference = unreserve_done.meta.reference
        callback = unreserve_done.meta.extensions.callback
    
        await self.log_to_reservation(reservation, f"Unreservation was acknowledged by Pod!", level=ProgressLevel.INFO)

        try:
            await self.forward(unreserve_done, callback)
            
            unprovision, provider = await get_unprovision_to_cause(unreserve_done)

            logger.info(f'Finally we can unprovide what we provided before')
            if unprovision is not None:
                await self.log_to_reservation(reservation, f"We can cause an Unprovision for this pod.. Causing Unprovide (not waiting for it)", level=ProgressLevel.INFO)
                await self.forward(unprovision, f"unprovision_in_{provider.unique}") #TODO: Should we do this before deleting the reservation?
            
            
            await end_reservation(unreserve_done.data.reservation)
            await self.log_to_reservation(reservation, f"Sucessfully Unprovided this", level=ProgressLevel.INFO)

        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.log_to_reservation(reservation, f"Protocol Error on Unreservation.. Done Processment", level=ProgressLevel.ERROR)
            await self.forward(exception, callback)


        await message.channel.basic_ack(message.delivery.delivery_tag)

    @BouncedAssignMessage.unwrapped_message
    async def on_bounced_assign_in(self, bounced_assign: BouncedAssignMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Assign {str(message.body.decode())}")
        assignation = await create_assignation_from_bounced_assign(bounced_assign)

        try:
            topic = await get_topic_for_bounced_assign(bounced_assign)
            await self.forward(bounced_assign, topic)
                
               
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, bounced_assign.meta.reference)
            await log_to_assignation(bounced_assign.meta.reference, str(e), level=LogLevel.ERROR)
            await set_assignation_status(bounced_assign.meta.reference, AssignationStatus.CRITICAL.value)
            await self.forward(exception, bounced_assign.meta.extensions.callback)
            console.print_exception()

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @BouncedUnassignMessage.unwrapped_message
    async def on_bounced_unassign_in(self, bounced_unassign: BouncedUnassignMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Unassign {str(message.body.decode())}")

        reference = bounced_unassign.meta.reference
        assignation = bounced_unassign.data.assignation
        callback =  bounced_unassign.meta.extensions.callback

        await self.log_to_assignation(assignation, f"Unreserve Request received", level=ProgressLevel.INFO)

        try:
            topic = await find_topic_for_unassignation(bounced_unassign)

            if topic: 
                # We first need to unprovide this Reservation
                await self.log_to_assignation(assignation, f"Sending unassignation to Pod", level=ProgressLevel.INFO)
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

                await self.log_to_assignation(assignation, f"Unassignation could not be delived to pod because pod is no longer active.. Probably it disconnected", level=ProgressLevel.ERROR)
                await self.forward(unreserve_done, callback)


                           
        except Exception as e:
            logger.error(e)
            exception = ExceptionMessage.fromException(e, reference)
            await self.log_to_assignation(assignation, f"Protocol Error on Unassignation {str(e)}", level=ProgressLevel.ERROR)
            await self.forward(exception, callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)



