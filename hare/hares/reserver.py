from facade.subscriptions.assignation import MyAssignationsEvent
from hare.scheduler.base import MessageEvent
from hare.transitions.provision import cancel_provision
from delt.messages.postman.provide.provide_transition import ProvideState
from hare.transitions.reservation import (
    cancel_reservation,
    critical_reservation,
    crititcal_reservation_by_reference,
    pause_reservation,
)
from re import M
from facade.subscriptions.provision import MyProvisionsEvent, ProvisionEventSubscription
from delt.messages.generics import Context
from facade.utils import (
    log_to_assignation,
    log_to_provision,
    log_to_reservation,
    set_assignation_status,
    transition_reservation,
    transition_reservation_by_reference,
)
from delt.messages.postman.provide.params import ProvideParams
from facade.enums import (
    AssignationStatus,
    LogLevel,
    PodStatus,
    ProvisionStatus,
    ReservationStatus,
    TopicStatus,
)
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages import *
from typing import List, Tuple, Union
from facade.models import Assignation, Provision, Reservation, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare
from hare.scheduler.default import DefaultScheduler

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


def events_for_assignment(bounced_assign: BouncedAssignMessage) -> List[MessageEvent]:
    """Preperaes messages for unprovision

    Args:
        bounced_unreserve (BouncedUnprovideMessage): [description]

    Raises:
        e: [description]

    """
    reservation = bounced_assign.data.reservation
    callback = bounced_assign.meta.extensions.callback
    reference = bounced_assign.meta.reference
    context = bounced_assign.meta.context
    res = Reservation.objects.get(reference=reservation)

    events = []
    if res.status == ReservationStatus.ACTIVE:
        assignation = Assignation.objects.get(reference=reference)
        assignation.status = AssignationStatus.ASSIGNED
        assignation.save()

        if assignation.creator:
            MyAssignationsEvent.broadcast(
                {"action": AssignationStatus.ASSIGNED.value, "data": assignation.id},
                [f"assignations_user_{assignation.creator.id}"],
            )
        events.append(MessageEvent(f"assignments_in_{res.channel}", bounced_assign))

    else:
        assignation = Assignation.objects.get(reference=reference)
        assignation.status = AssignationStatus.CRITICAL
        assignation.save()

        if assignation.creator:
            MyAssignationsEvent.broadcast(
                {"action": AssignationStatus.CRITICAL.value, "data": assignation.id},
                [f"assignations_user_{assignation.creator.id}"],
            )

        assign_critical = AssignCriticalMessage(
            data={"type": "DeniedError", "message": "Assignment was denied"},
            meta={"reference": bounced_assign.meta.reference},
        )

        events.append(MessageEvent(callback, assign_critical))

    return events


def events_for_unassignment(
    bounced_unassign: BouncedUnassignMessage,
) -> List[MessageEvent]:
    """Preperaes messages for unprovision

    Args:
        bounced_unreserve (BouncedUnprovideMessage): [description]

    Raises:
        e: [description]

    """
    provision_reference = bounced_unassign.data.provision
    callback = bounced_unassign.meta.extensions.callback
    reference = bounced_unassign.meta.reference
    prov = Provision.objects.get(reference=provision_reference)

    events = []
    if prov.status == ProvisionStatus.ACTIVE:
        events.append(
            MessageEvent(f"cancel_assign_{provision_reference}", bounced_unassign)
        )

    else:
        assignation = Assignation.objects.get(reference=reference)
        assignation.status = AssignationStatus.CRITICAL
        assignation.save()

        if assignation.creator:
            MyAssignationsEvent.broadcast(
                {"action": AssignationStatus.CRITICAL.value, "data": assignation.id},
                [f"assignations_user_{assignation.creator.id}"],
            )

        assign_critical = AssignCriticalMessage(
            data={
                "type": "DeniedError",
                "message": "Provision was not active anymore. That shouldn't happen",
            },
            meta={"reference": bounced_unassign.meta.reference},
        )

        events.append(MessageEvent(callback, assign_critical))

    return events


scheduler = DefaultScheduler()


class ReserverRabbit(BaseHare):
    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.bounced_reserve_in = await self.channel.queue_declare("bounced_reserve_in")
        self.bounced_unreserve_in = await self.channel.queue_declare(
            "bounced_unreserve_in"
        )
        self.bounced_unprovide_in = await self.channel.queue_declare(
            "bounced_unprovide_in"
        )
        self.unreserve_done_in = await self.channel.queue_declare("unreserve_done_in")

        self.bounced_assign_in = await self.channel.queue_declare("bounced_assign_in")
        self.bounced_unassign_in = await self.channel.queue_declare(
            "bounced_unassign_in"
        )

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(
            self.bounced_reserve_in.queue, self.on_bounced_reserve_in
        )
        await self.channel.basic_consume(
            self.bounced_unreserve_in.queue, self.on_bounced_unreserve_in
        )
        await self.channel.basic_consume(
            self.bounced_unprovide_in.queue, self.on_bounced_unprovide_in
        )
        await self.channel.basic_consume(
            self.bounced_assign_in.queue, self.on_bounced_assign_in
        )
        await self.channel.basic_consume(
            self.bounced_unassign_in.queue, self.on_bounced_unassign_in
        )

    @BouncedReserveMessage.unwrapped_message
    async def on_bounced_reserve_in(
        self,
        bounced_reserve: BouncedReserveMessage,
        aiomessage: aiormq.abc.DeliveredMessage,
    ):
        """Bounced Reserve In

        Bounced reserve will only be called once a Reservation has already been called with a reference to the created
        reservation

        Args:
            bounced_reserve (BouncedReserveMessage): [description]
            message (aiormq.abc.DeliveredMessage): [description]

        """
        try:

            logger.info(
                f"Received Bounced Reserve {str(aiomessage.body.decode())} {bounced_reserve}"
            )

            events = await sync_to_async(scheduler.on_reserve)(bounced_reserve)

            for event in events:
                logger.info(f"EVENT {event}")
                if event.channel:
                    await self.forward(event.message, event.channel)

        except Exception as e:
            logger.exception(e)
            reference = bounced_reserve.meta.reference
            messages = await sync_to_async(crititcal_reservation_by_reference)(
                reference, f"Error: {str(e)}"
            )
            for channel, message in messages:
                if channel:
                    await self.forward(message, channel)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    @BouncedUnreserveMessage.unwrapped_message
    async def on_bounced_unreserve_in(
        self,
        bounced_unreserve: BouncedUnreserveMessage,
        aiomessage: aiormq.abc.DeliveredMessage,
    ):
        try:

            logger.info(
                f"Received Bounced Unreserve {str(aiomessage.body.decode())} {bounced_reserve}"
            )

            events = await sync_to_async(scheduler.on_unreserve)(bounced_unreserve)

            for event in events:
                logger.info(f"EVENT {event}")
                if event.channel:
                    await self.forward(event.message, event.channel)

        except Exception as e:
            logger.exception(e)

        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    @BouncedUnprovideMessage.unwrapped_message
    async def on_bounced_unprovide_in(
        self,
        bounced_unreserve: BouncedUnprovideMessage,
        aiomessage: aiormq.abc.DeliveredMessage,
    ):
        try:

            logger.info(
                f"Received Bounced Unprovide {str(aiomessage.body.decode())} {bounced_unreserve}"
            )

            reference = bounced_unreserve.meta.reference
            callback = bounced_unreserve.meta.extensions.callback

            try:
                messages = await sync_to_async()(bounced_unreserve)

                for channel, message in messages:
                    logger.info(f"Sending {message} to {channel}")
                    await self.forward(message, channel)

            except ProtocolException as e:
                logger.exception(e)
                exception = ExceptionMessage.fromException(e, reference)
                await sync_to_async(log_to_provision)(
                    reference, f"Unreservation Error: {str(e)}", level=LogLevel.ERROR
                )
                if callback:
                    await self.forward(exception, callback)

        except Exception as e:
            logger.exception(e)
            exception = ExceptionMessage.fromException(e, reference)

        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    @BouncedAssignMessage.unwrapped_message
    async def on_bounced_assign_in(
        self,
        bounced_assign: BouncedAssignMessage,
        aiomessage: aiormq.abc.DeliveredMessage,
    ):
        logger.info(f"Received Bounced Assign {str(aiomessage.body.decode())}")

        reference = bounced_assign.meta.reference
        callback = bounced_assign.meta.extensions.callback

        try:
            events = await sync_to_async(events_for_assignment)(bounced_assign)
            for event in events:
                logger.info(f"EVENT {event}")
                if event.channel:
                    await self.forward(event.message, event.channel)

        except Exception as e:
            logger.exception(e)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    @BouncedUnassignMessage.unwrapped_message
    async def on_bounced_unassign_in(
        self,
        bounced_unassign: BouncedUnassignMessage,
        aiomessage: aiormq.abc.DeliveredMessage,
    ):
        logger.info(f"Received Bounced Unassign {str(aiomessage.body.decode())}")

        reference = bounced_unassign.meta.reference
        assignation = bounced_unassign.data.assignation
        callback = bounced_unassign.meta.extensions.callback

        try:
            events = await sync_to_async(events_for_unassignment)(bounced_unassign)

            for event in events:
                if event.channel:
                    await self.forward(event.message, event.channel)

        except Exception as e:
            logger.error(e)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)
