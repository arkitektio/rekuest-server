from enum import Enum
from delt.events import ProvisionTransitionEvent
from delt.messages.postman.unprovide.bounced_unprovide import BouncedUnprovideMessage
from delt.types import ReserveParams
from hare.scheduler.default.utils import predicate_unprovide
from hare.trans import (
    agent_activate_provision,
    agent_add_reservation_to_provision,
    agent_bind_provision,
    agent_cancel_provision,
    agent_critical_provision,
    agent_disconnect,
    agent_disconnect_provision,
    agent_remove_reservation_from_provision,
    agent_set_providing_provision,
)
from hare.transitions.assignation import (
    cancel_assignation_by_reference,
    critical_assignation_by_reference,
    done_assignation_by_reference,
    log_to_assignation_by_reference,
    receive_assignation_by_reference,
    return_assignation_by_reference,
    yield_assignation_by_reference,
)
from delt.messages.postman.assign.assign_cancelled import AssignCancelledMessage
from delt.messages.postman.assign.assign_done import AssignDoneMessage
from delt.messages.postman.assign.assign_return import AssignReturnMessage
from delt.messages.postman.assign.assign_critical import AssignCriticalMessage
from delt.messages.postman.assign.assign_log import AssignLogMessage
from delt.messages.postman.assign.assign_yield import AssignYieldsMessage
from hare.transitions.base import TransitionException
from delt.messages.postman.provide.provide_log import ProvideLogMessage
from delt.messages.types import BOUNCED_FORWARDED_ASSIGN, BOUNCED_FORWARDED_UNASSIGN
from delt.messages.postman.unassign.bounced_unassign import BouncedUnassignMessage
from delt.messages.postman.assign.bounced_forwarded_assign import (
    BouncedForwardedAssignMessage,
)
from delt.messages.postman.unassign.bounced_forwarded_unassign import (
    BouncedForwardedUnassignMessage,
)
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.provide.provide_transition import (
    ProvideState,
    ProvideTransistionData,
)
from delt.messages import (
    BouncedUnreserveMessage,
    AssignReceivedMessage,
    BouncedProvideMessage,
)
from facade.utils import log_to_provision, transition_provision
from facade.consumers.base import BaseConsumer
from asgiref.sync import sync_to_async
from facade.helpers import (
    create_assignation_from_bouncedassign,
    create_bounced_assign_from_assign,
    create_bounced_reserve_from_reserve,
    create_reservation_from_bouncedreserve,
    get_channel_for_reservation,
)
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
import aiormq
from delt.messages.exception import ExceptionMessage
from delt.messages.base import MessageModel
from delt.messages import (
    ProvideTransitionMessage,
    ProvideCriticalMessage,
    ReserveTransitionMessage,
)
from channels.generic.websocket import AsyncWebsocketConsumer
from delt.messages.utils import expandFromRabbitMessage, expandToMessage, MessageError
import json
import logging
from lok.bouncer.utils import bounced_ws
import asyncio
from ..models import Agent, Provision, Registry, Reservation
from facade.subscriptions.agent import AgentsEvent
from facade.subscriptions.reservation import MyReservationsEvent
from facade.subscriptions.provision import MyProvisionsEvent
from facade.enums import AgentStatus, ProvisionStatus, ReservationStatus
from arkitekt.console import console
from hare.transitions.reservation import (
    activate_reservation,
    disconnect_reservation,
    critical_reservation,
    cancel_reservation,
    log_event_to_reservation,
)
from hare.transitions.provision import (
    log_to_provision_by_reference,
)

logger = logging.getLogger(__name__)


def activate_and_add_reservation_to_provision_by_reference(
    res_reference: str, prov_reference: str
):
    res = Reservation.objects.get(reference=res_reference)
    prov = Provision.objects.get(reference=prov_reference)
    return agent_add_reservation_to_provision(res, prov)


def cancel_and_delete_reservation_from_provision_by_reference(
    res_reference: str, prov_reference: str
):

    res = Reservation.objects.get(reference=res_reference)
    prov = Provision.objects.get(reference=prov_reference)

    events = agent_remove_reservation_from_provision(res, prov)

    return events


def activate_provision_by_reference(provide_transition: ProvideTransitionMessage):
    provision = Provision.objects.get(reference=provide_transition.meta.reference)
    return agent_activate_provision(provision, provide_transition)


def critical_provision_by_reference(provide_transition: ProvideTransitionMessage):
    provision = Provision.objects.get(reference=provide_transition.meta.reference)
    return agent_critical_provision(provision, provide_transition)


def set_providing_provision_by_reference(provide_transition: ProvideTransitionMessage):
    provision = Provision.objects.get(reference=provide_transition.meta.reference)
    return agent_set_providing_provision(provision, provide_transition)


def cancel_provision_by_reference(provide_transition: ProvideTransitionMessage):
    provision = Provision.objects.get(reference=provide_transition.meta.reference)
    return agent_cancel_provision(provision, provide_transition)


def bind_provision_to_agent(provide: BouncedProvideMessage, agent: Agent):
    provision = Provision.objects.get(reference=provide.meta.reference)
    return agent_bind_provision(provision, agent, provide)


class AlreadyActiveAgent(Exception):
    pass


def activate_agent_and_get_active_provisions(app, user, identifier="main"):

    if user is None or user.is_anonymous:
        registry = Registry.objects.get(app=app, user=None)
    else:
        registry = Registry.objects.get(app=app, user=user)

    agent, created = Agent.objects.get_or_create(
        registry=registry,
        identifier=identifier,
        defaults={"name": f"{registry} on {identifier}"},
    )

    if agent.status == AgentStatus.ACTIVE:
        raise AlreadyActiveAgent(
            "There is an already active agent this is not what we want"
        )

    agent.status = AgentStatus.ACTIVE
    agent.save()

    if agent.registry.user:
        AgentsEvent.broadcast(
            {"action": "updated", "data": str(agent.id)},
            [f"agents_user_{agent.registry.user.id}"],
        )
    else:
        AgentsEvent.broadcast({"action": "updated", "data": agent.id}, [f"all_agents"])

    provisions = (
        Provision.objects.filter(bound=agent)
        .exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED])
        .all()
    )

    requests = []
    for prov in provisions:
        requests.append(prov.to_message())

    return agent, registry, requests


class WebsocketCodes(int, Enum):
    ALREADY_CONNECTED_IDENTIFER = 4001


def reset_agents():
    for agent in Agent.objects.all():
        agent.status = AgentStatus.VANILLA
        agent.save()


def reset_provisions():
    for provisions in Provision.objects.filter(status=ProvisionStatus.ACTIVE).all():
        provisions.status = ProvisionStatus.DISCONNECTED
        provisions.save()


reset_agents()
reset_provisions()


class AgentConsumer(BaseConsumer):  # TODO: Seperate that bitch
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

        try:
            self.agent, self.registry, start_provisions = await sync_to_async(
                activate_agent_and_get_active_provisions
            )(self.scope["bounced"].app, self.scope["bounced"].user)
        except AlreadyActiveAgent as e:

            logger.error(f"Blocking scosndasry Connection attempt on same idenfitifer")
            await self.close(code=WebsocketCodes.ALREADY_CONNECTED_IDENTIFER.value)
            return

        logger.warning(f"Connecting {self.agent.name}")

        self.provision_link_map = {}  # A link with all the queue indexed by provision
        self.assignments_tag_map = {}
        self.assignments_channel_map = {}

        await self.connect_to_rabbit()

        for prov in start_provisions:
            logger.error(f"Sending {prov} to Agent")
            await self.send_message(prov)

    async def disconnect(self, close_code):
        if close_code == WebsocketCodes.ALREADY_CONNECTED_IDENTIFER:
            logger.error(
                f"Disconnecting connection because we already have an agent running on this identifier"
            )
            return

        try:
            logger.warning(
                f"Disconnecting Agent {self.agent.name} with close_code {close_code}"
            )
            # We are deleting all associated Provisions for this Agent
            events = await sync_to_async(agent_disconnect)(self.agent)

            for event in events:
                if event.channel:
                    await self.forward(event.message, event.channel)

            await self.connection.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # Declaring queue
        self.on_provide_queue = await self.channel.queue_declare(
            f"provision_agent_in_{self.agent.unique}", auto_delete=True
        )
        self.on_all_provide_queue = await self.channel.queue_declare(
            f"provision_registry_in_{self.agent.registry.unique}", auto_delete=True
        )

        await self.channel.basic_consume(
            self.on_provide_queue.queue, self.on_provide_related
        )
        await self.channel.basic_consume(
            self.on_all_provide_queue.queue, self.on_provide_related
        )

    async def on_provide_related(self, aiomessage: aiormq.abc.DeliveredMessage):
        """Provide Forward

        Simply forwards provide messages to the Agent on the Other end

        Args:
            message (aiormq.abc.DeliveredMessage): The delivdered message
        """
        message = expandFromRabbitMessage(aiomessage)
        try:
            if isinstance(message, BouncedProvideMessage):
                await sync_to_async(bind_provision_to_agent)(message, self.agent)
                await self.send(text_data=aiomessage.body.decode())

            if isinstance(message, BouncedUnprovideMessage):
                await self.send(text_data=aiomessage.body.decode())

        except Exception as e:
            logger.exception(e)

        # No need to go through pydantic???
        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    async def on_reservation_related(
        self, provision_reference, aiomessage: aiormq.abc.DeliveredMessage
    ):
        message = expandFromRabbitMessage(aiomessage)

        try:
            if isinstance(message, BouncedReserveMessage):
                reservation_reference = message.meta.reference
                events, assignment_topic = await sync_to_async(
                    activate_and_add_reservation_to_provision_by_reference
                )(reservation_reference, provision_reference)
                # Set up connectiongs
                assert (
                    provision_reference in self.provision_link_map
                ), "Provision is not provided"
                assign_queue = await self.channel.queue_declare(assignment_topic)

                await self.channel.basic_consume(
                    assign_queue.queue,
                    lambda x: self.on_assign_related(provision_reference, x),
                )
                self.provision_link_map[provision_reference].append(assign_queue)

                for event in events:
                    if event.channel:
                        await self.forward(event.message, event.channel)

            elif isinstance(message, BouncedUnreserveMessage):
                reservation_reference = message.data.reservation
                events = await sync_to_async(
                    cancel_and_delete_reservation_from_provision_by_reference
                )(reservation_reference, provision_reference)

                for event in events:
                    if event.channel:
                        await self.forward(event.message, event.channel)

            else:
                raise Exception("This message is not what we expeceted here")

        except Exception as e:
            logger.exception(e)

        await aiomessage.channel.basic_ack(aiomessage.delivery.delivery_tag)

    async def on_assign_related(
        self, provision_reference, message: aiormq.abc.DeliveredMessage
    ):
        nana = expandFromRabbitMessage(message)
        logger.info(message.delivery.delivery_tag)

        if isinstance(nana, BouncedAssignMessage):
            forwarded_message = BouncedForwardedAssignMessage(
                data={**nana.data.dict(), "provision": provision_reference},
                meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_ASSIGN},
            )
            assign_received = AssignReceivedMessage(
                data={"provision": provision_reference},
                meta=nana.meta.dict(exclude={"type"}),
            )
            if nana.meta.extensions.persist == True:
                await sync_to_async(receive_assignation_by_reference)(
                    nana.meta.reference, provision_reference
                )
            await self.forward(assign_received, nana.meta.extensions.callback)
            await self.send_message(
                forwarded_message
            )  # No need to go through pydantic???

        elif isinstance(nana, BouncedUnassignMessage):
            forwarded_message = BouncedForwardedUnassignMessage(
                data={**nana.data.dict(), "provision": provision_reference},
                meta={**nana.meta.dict(), "type": BOUNCED_FORWARDED_UNASSIGN},
            )
            await self.send_message(forwarded_message)

        else:
            logger.error("This message is not what we expeceted here")

        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def on_provide_transition(self, provide_transition: ProvideTransitionMessage):

        provision_reference = provide_transition.meta.reference
        new_state = provide_transition.data.state
        events = []
        try:
            if new_state == ProvideState.ACTIVE:

                events, reservation_topic, assignment_topics = await sync_to_async(
                    activate_provision_by_reference
                )(provide_transition)

                logger.info(f"Listening to {reservation_topic}")
                reservation_queue = await self.channel.queue_declare(reservation_topic)
                cancellation_queue = await self.channel.queue_declare(
                    f"cancel_assign_{provision_reference}"
                )

                await self.channel.basic_consume(
                    cancellation_queue.queue,
                    lambda x: self.on_assign_related(provision_reference, x),
                )

                await self.channel.basic_consume(
                    reservation_queue.queue,
                    lambda x: self.on_reservation_related(provision_reference, x),
                )

                for channel in assignment_topics:
                    assign_queue = await self.channel.queue_declare(channel)
                    await self.channel.basic_consume(
                        assign_queue.queue,
                        lambda x: self.on_assign_related(provision_reference, x),
                    )
                    self.provision_link_map.setdefault(provision_reference, []).append(
                        assign_queue
                    )

            elif new_state == ProvideState.CRITICAL:
                events = await sync_to_async(critical_provision_by_reference)(
                    provide_transition
                )

            elif new_state == ProvideState.CANCELLED:
                events = await sync_to_async(cancel_provision_by_reference)(
                    provide_transition
                )

            elif new_state == ProvideState.PROVIDING:
                events = await sync_to_async(set_providing_provision_by_reference)(
                    provide_transition
                )

            else:
                logger.error(f"Unkown Message {new_state}")

            for event in events:
                if event.channel:
                    logger.error(event)
                    await self.forward(event.message, event.channel)

        except TransitionException as e:
            logger.error(e)

        except Exception as e:
            logger.exception(e)

    async def on_provide_log(self, provide_log: ProvideLogMessage):
        # TODO: End true statement here
        if provide_log.meta.extensions.persist == True or True:
            await sync_to_async(log_to_provision_by_reference)(
                provide_log.meta.reference,
                provide_log.data.message,
                provide_log.data.level,
            )
        await self.forward(provide_log, provide_log.meta.extensions.progress)

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        if assign_yield.meta.extensions.persist == True:
            await sync_to_async(yield_assignation_by_reference)(
                assign_yield.meta.reference, assign_yield.data.returns
            )
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_critical(self, assign_critical: AssignCriticalMessage):
        if assign_critical.meta.extensions.persist == True:
            await sync_to_async(critical_assignation_by_reference)(
                assign_critical.meta.reference, message=assign_critical.data.message
            )
        await self.forward(assign_critical, assign_critical.meta.extensions.callback)

    async def on_assign_cancelled(self, assign_cancelled: AssignCancelledMessage):
        if assign_cancelled.meta.extensions.persist == True:
            await sync_to_async(cancel_assignation_by_reference)(
                assign_cancelled.meta.reference
            )
        await self.forward(assign_cancelled, assign_cancelled.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        if assign_return.meta.extensions.persist == True:
            await sync_to_async(return_assignation_by_reference)(
                assign_return.meta.reference, assign_return.data.returns
            )
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_done(self, assign_done: AssignDoneMessage):
        if assign_done.meta.extensions.persist == True:
            await sync_to_async(done_assignation_by_reference)(
                assign_done.meta.reference
            )
        await self.forward(assign_done, assign_done.meta.extensions.callback)

    async def on_assign_log(self, assign_log: AssignLogMessage):
        if assign_log.meta.extensions.persist == True:
            await sync_to_async(log_to_assignation_by_reference)(
                assign_log.meta.reference,
                assign_log.data.message,
                assign_log.data.level,
            )
        await self.forward(assign_log, assign_log.meta.extensions.progress)
