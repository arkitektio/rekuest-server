from typing import List
from delt.events import ProvisionTransitionEvent
from delt.messages.postman.log import LogLevel
from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from delt.messages.postman.provide.provide_transition import (
    ProvideState,
    ProvideTransitionMessage,
)
from delt.messages.postman.reserve.reserve_log import ReserveLogMessage
from delt.messages.postman.reserve.reserve_transition import (
    ReserveState,
    ReserveTransitionMessage,
)
from delt.messages.postman.unprovide.bounced_unprovide import BouncedUnprovideMessage
from delt.types import ProvideParams, ReserveParams
from facade.enums import AgentStatus, ProvisionStatus, ReservationStatus
from facade.models import Agent, Reservation, Provision
from facade.subscriptions.agent import AgentsEvent
from facade.subscriptions.provision import MyProvisionsEvent
from facade.subscriptions.reservation import MyReservationsEvent
from hare.scheduler.base import MessageEvent
import logging

from hare.scheduler.default.utils import predicate_unprovide
from hare.transitions.base import TransitionException
from hare.transitions.reservation import log_event_to_reservation


logger = logging.getLogger(__name__)


def agent_disconnect_reservation(
    res: Reservation, message: str = None, reconnect=False
) -> List[MessageEvent]:
    """Disconnect a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    res.status = ReserveState.DISCONNECT
    res.save()
    events = []

    if res.callback:
        events.append(
            MessageEvent(
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.DISCONNECT, "message": message},
                    meta={
                        "reference": res.reference,
                    },
                ),
            )
        )

        if reconnect:
            logger.error("Reconnection not implemented yet")
            # TODO: Implement a Reconnecting algorithm that tries to find another assignable node (through reserver)

    if res.creator:
        MyReservationsEvent.broadcast(
            {"action": ReserveState.DISCONNECT.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return events


def agent_activate_reservation(
    res: Reservation, message: str = None
) -> List[MessageEvent]:
    """Activae Reservation

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    res.status = ReserveState.ACTIVE
    res.save()
    events = []

    if res.callback:
        events.append(
            MessageEvent(
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.ACTIVE, "message": message},
                    meta={
                        "reference": res.reference,
                    },
                ),
            )
        )

    if res.creator:
        MyReservationsEvent.broadcast(
            {"action": ReserveState.ACTIVE.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return events


def agent_activate_provision(
    prov: Provision, message: ProvideTransitionMessage
) -> List[MessageEvent]:
    """Activate Provision

    Takes an provision Activates it and returns the
    channels and additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    events = []
    prov.status = ProvideState.ACTIVE
    prov.mode = message.data.mode
    prov.statusmessage = message.data.message
    prov.save()

    if prov.callback:
        events.append(MessageEvent(prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": "updated", "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    reservation_topic = f"reservations_in_{prov.unique}"  # The channel we will use to listen to for Reservations
    assignment_topics = []

    for res in prov.reservations.all():
        try:
            logger.info(f"[green] Listening to {res}")

            res_params = ReserveParams(**res.params)

            viable_provisions_amount = min(
                res_params.minimalInstances, res_params.desiredInstances
            )

            logger.error(f"MINIMAL VIABALE AMOUNT {viable_provisions_amount}")

            res_active_provisions = res.provisions.filter(
                status=ProvisionStatus.ACTIVE
            ).count()

            if res_active_provisions >= viable_provisions_amount:
                events += agent_activate_reservation(
                    res, f"Activated by activated provision {prov}"
                )

            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )

            assignment_topic = f"assignments_in_{res.channel}"
            assignment_topics.append(assignment_topic)

        except TransitionException as e:
            logger.exception(e)

    return events, reservation_topic, assignment_topics


def agent_disconnect_provision(
    prov: Provision, message: str = "hahaha"
) -> List[MessageEvent]:
    """Disconnect Provision

    Takes an active provision and disconnects it
    and returns additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    prov.status = ProvideState.DISCONNECTED
    prov.save()

    events = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_params = ReserveParams(**res.params)
            viable_provisions_amount = min(
                res_params.minimalInstances, res_params.desiredInstances
            )

            active_provisions_counts = res.provisions.filter(
                status=ProvisionStatus.ACTIVE
            ).count()
            if active_provisions_counts < viable_provisions_amount:
                events += agent_disconnect_reservation(
                    res, message=f"Disconnected by disconnected provision {prov}"
                )

            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
        except TransitionException:
            pass

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.DISCONNECTED.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return events


def agent_add_reservation_to_provision(
    res: Reservation, prov: Provision
) -> List[MessageEvent]:
    prov.reservations.add(res)
    prov.save()
    events = []

    res_params = ReserveParams(**res.params)

    viable_provisions_amount = min(
        res_params.minimalInstances, res_params.desiredInstances
    )

    assignment_topic = f"assignments_in_{res.channel}"

    logger.error(f"MINIMAL VIABALE AMOUNT {viable_provisions_amount}")

    active_provisions = res.provisions.filter(status=ProvisionStatus.ACTIVE).count()

    if active_provisions >= viable_provisions_amount:
        res.status = ReserveState.ACTIVE
        res.save()

        if res.callback:
            events.append(
                MessageEvent(
                    res.callback,
                    ReserveTransitionMessage(
                        data={
                            "state": ReserveState.ACTIVE,
                            "message": "Minimal Viable Amount is online!",
                        },
                        meta={
                            "reference": res.reference,
                        },
                    ),
                )
            )

        if res.creator:
            MyReservationsEvent.broadcast(
                {"action": ReserveState.ACTIVE.value, "data": res.id},
                [f"reservations_user_{res.creator.id}"],
            )

    log_event_to_reservation(
        res,
        ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
    )

    return events, assignment_topic


def agent_critical_reservation(
    res: Reservation, message: str = None, reconnect=False
) -> List[MessageEvent]:
    """Criticals a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    res.status = ReserveState.CRITICAL
    res.save()
    events = []

    if res.callback:
        events.append(
            MessageEvent(
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.CRITICAL, "message": message},
                    meta={
                        "reference": res.reference,
                    },
                ),
            )
        )

    if res.creator:
        MyReservationsEvent.broadcast(
            {"action": ReserveState.CRITICAL.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return events


def agent_bind_provision(
    prov: Provision, agent: Agent, message: BouncedProvideMessage
) -> List[MessageEvent]:
    """Disconnect Provision

    Takes an active provision and disconnects it
    and returns additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if prov.status in [ProvideState.CANCELLED, ProvideState.ENDED]:
        raise TransitionException(
            f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    prov.bound = agent
    prov.status = ProvisionStatus.BOUND
    prov.save()

    for res in prov.reservations.all():
        try:
            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
        except TransitionException:
            pass

    events = []

    if prov.callback:
        events.append(MessageEvent(prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvisionStatus.BOUND.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return events


def agent_cancel_provision(
    prov: Provision, message: ProvideTransitionMessage
) -> List[MessageEvent]:
    """Disconnect Provision

    Takes an active provision and disconnects it
    and returns additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if prov.status in [ProvideState.CANCELLED, ProvideState.ENDED]:
        raise TransitionException(
            f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    prov.status = ProvideState.CANCELLED
    prov.save()

    events = []
    assignment_topics = []

    for res in prov.reservations.all():
        logger.info(f"Cancelling {res}")
        res_params = ReserveParams(**res.params)

        viable_provisions_amount = min(
            res_params.minimalInstances, res_params.desiredInstances
        )

        logger.error(f"MINIMAL VIABALE AMOUNT {viable_provisions_amount}")

        res_active_provisions = res.provisions.filter(
            status=ProvisionStatus.ACTIVE
        ).count()

        if res_active_provisions >= viable_provisions_amount:
            events += agent_critical_reservation(
                res, f"Activated by activated provision {prov}"
            )

        log_event_to_reservation(
            res,
            ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
        )

        assignment_topic = f"assignments_in_{res.channel}"
        assignment_topics.append(assignment_topic)

    if prov.callback:
        events.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.CANCELLED.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return events, assignment_topics


def agent_set_providing_provision(
    prov: Provision, message: ProvideTransitionMessage
) -> List[MessageEvent]:
    """Disconnect Provision

    Takes an active provision and disconnects it
    and returns additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    prov.status = ProvideState.PROVIDING
    prov.save()

    events = []

    for res in prov.reservations.all():
        logger.info(f"Sending a log to the reservations {res}")

        log_event_to_reservation(
            res,
            ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
        )

    if prov.callback:
        events.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.PROVIDING.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return events


def agent_critical_provision(prov: Provision, message: ProvideTransitionMessage):
    """Critical Provision

    Takes an active provision and disconnects it
    and returns additional messages that need to
    be send

    Args:
        prov (Provision): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if prov.status in [ProvideState.CANCELLED, ProvideState.ENDED]:
        raise TransitionException(
            f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    prov.status = ProvideState.CRITICAL
    prov.save()

    events = []

    for res in prov.reservations.all():
        logger.info(f"Criticalling {res}")
        res_params = ReserveParams(**res.params)

        viable_provisions_amount = min(
            res_params.minimalInstances, res_params.desiredInstances
        )

        logger.error(f"MINIMAL VIABALE AMOUNT {viable_provisions_amount}")

        res_active_provisions = res.provisions.filter(
            status=ProvisionStatus.ACTIVE
        ).count()

        if res_active_provisions >= viable_provisions_amount:
            events += agent_critical_reservation(
                res, f"Activated by activated provision {prov}"
            )

        log_event_to_reservation(
            res,
            ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
        )

    if prov.callback:
        events.append(MessageEvent(prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.CRITICAL.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return events


def agent_remove_reservation_from_provision(
    reservation: Reservation, prov: Provision
) -> List[MessageEvent]:
    """Removes a Reservation from a Provision

    It will also check if the provision should then be autounprovided if
    there is no longer a reservation attached to it.

    Attention: it will not set the reservation to false. Maybe this needs to
    happen as well?

    Args:
        reservation (Reservation): [description]
        prov (Provision): [description]

    Returns:
        List[MessageEvent]: [description]
    """
    events = []

    params = ProvideParams(**prov.params)
    prov.reservations.remove(reservation)

    reserve_log = ReserveLogMessage(
        data={
            "level": LogLevel.INFO,
            "message": f"We removed the Reservation {reservation.reference} from {prov.reference}",
        },
        meta={"reference": reservation.reference},
    )

    events.append(MessageEvent(reservation.callback, reserve_log))

    if params.autoUnprovide:
        logger.info("Autounprovide  was set to True. Checking if needs to unprovide")
        if predicate_unprovide(reservation, prov):
            if prov.status == ProvisionStatus.ACTIVE:
                prov.status = ProvisionStatus.CANCELING

                if prov.creator:
                    MyProvisionsEvent.broadcast(
                        {"action": "updated", "data": prov.id},
                        [f"provisions_user_{prov.creator.id}"],
                    )

                unprovide_message = BouncedUnprovideMessage(
                    data={
                        "provision": prov.reference,
                    },
                    meta={
                        "reference": reservation.reference,
                        "extensions": reservation.extensions,
                        "context": reservation.context,
                    },
                )

                reserve_log = ReserveLogMessage(
                    data={
                        "level": LogLevel.INFO,
                        "message": f"Provision {prov} has no other reservations attached and autoUnprovide was set to True but we need to make it go away first!",
                    },
                    meta={"reference": reservation.reference},
                )

                events.append(MessageEvent(reservation.callback, reserve_log))
                events.append(
                    MessageEvent(
                        f"provision_agent_in_{prov.bound.unique}", unprovide_message
                    )
                )

            else:
                prov.status = ProvisionStatus.CANCELLED
                prov.save()

                if prov.creator:
                    MyProvisionsEvent.broadcast(
                        {"action": "updated", "data": prov.id},
                        [f"provisions_user_{prov.creator.id}"],
                    )

                reserve_log = ReserveLogMessage(
                    data={
                        "level": LogLevel.INFO,
                        "message": f"Provision {prov} has no other reservations attached and autoUnprovide was set to True. Also Agent is offline. Voila it is gone!",
                    },
                    meta={"reference": reservation.reference},
                )

                events.append(MessageEvent(reservation.callback, reserve_log))

    if reservation.provisions.count() == 0:
        reservation.status = ReservationStatus.CANCELLED
        reservation.save()

        if reservation.creator:
            MyReservationsEvent.broadcast(
                {"action": "updated", "data": reservation.id},
                [f"reservations_user_{reservation.creator.id}"],
            )

        reserve_log = ReserveLogMessage(
            data={
                "level": LogLevel.INFO,
                "message": f"All Provisions for this Reservation have been removed. We are now saying goodby",
            },
            meta={"reference": reservation.reference},
        )

        reserve_transition = ReserveTransitionMessage(
            data={
                "state": ReserveState.CANCELLED,
                "message": f"All Provisions for this Reservation have been removed. We are now saying goodby",
            },
            meta={"reference": reservation.reference},
        )

        events.append(MessageEvent(reservation.callback, reserve_log))
        events.append(MessageEvent(reservation.callback, reserve_transition))

    return events


def agent_disconnect(agent: Agent):
    agent.status = AgentStatus.DISCONNECTED
    agent.save()

    provisions = (
        Provision.objects.filter(bound=agent)
        .exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED])
        .all()
    )

    events = []

    for provision in provisions:
        events += agent_disconnect_provision(
            provision, message="Disconnected trying to reconnect"
        )

    if agent.registry.user:
        AgentsEvent.broadcast(
            {"action": "updated", "data": agent.id},
            [f"agents_user_{agent.registry.user.id}"],
        )
    else:
        AgentsEvent.broadcast({"action": "updated", "data": agent.id}, [f"all_agents"])

    return events
