import json
from delt.events.base import Event
from facade.subscriptions.reservation import (
    MyReservationsEvent,
    ReservationEventSubscription,
)
from delt.messages.postman.reserve.reserve_transition import (
    ReserveState,
    ReserveTransitionMessage,
)
from hare.transitions.base import TransitionException
from facade.models import Reservation, ReservationLog
from facade.enums import LogLevel

import logging

logger = logging.getLogger(__name__)


def activate_reservation(res: Reservation, message: str = None):
    """Activae Reservation

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """
    if res.status == ReserveState.ACTIVE:
        # raise TransitionException(f"Reservation {res} was already active. Operation omitted to ensure Idempotence")
        pass

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    res.status = ReserveState.ACTIVE
    res.save()
    messages = []

    if res.callback:
        messages.append(
            (
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

    return messages


def disconnect_reservation(res: Reservation, message: str = None, reconnect=False):
    """Disconnect a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    res.status = ReserveState.DISCONNECT
    res.save()
    messages = []

    if res.callback:
        messages.append(
            (
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

    return messages


def crititcal_reservation_by_reference(reference, *args, **kwargs):
    return critical_reservation(
        Reservation.objects.get(reference=reference), *args, **kwargs
    )


def critical_reservation(res: Reservation, message: str = None, reconnect=False):
    """Criticals a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    res.status = ReserveState.CRITICAL
    res.save()
    messages = []

    if res.callback:
        messages.append(
            (
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

    return messages


def cancel_reservation(res: Reservation, message: str = None, reconnect=False):
    """Cancels a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to cancel it."
        )

    res.status = ReserveState.CANCELLED
    res.save()
    messages = []

    if res.callback:
        messages.append(
            (
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.CANCELLED, "message": message},
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
            {"action": ReserveState.CANCELLED.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return messages


def canceling_reservation(res: Reservation, message: str = None, reconnect=False):
    """Cancels a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to cancel it."
        )

    res.status = ReserveState.CANCELING
    res.save()
    messages = []

    if res.callback:
        messages.append(
            (
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.CANCELING, "message": message},
                    meta={
                        "reference": res.reference,
                    },
                ),
            )
        )

        if reconnect:
            logger.error("Reconnection not implemented yet")

    if res.creator:
        MyReservationsEvent.broadcast(
            {"action": ReserveState.CANCELING.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return messages


def pause_reservation(res: Reservation, message: str = None, reconnect=False):
    """Cancels a reservation

    Right now no reconnection attempt will be possible

    Args:
        res (Reservation): [description]

    Raises:
        TransitionException: [description]
        TransitionException: [description]
    """

    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(
            f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to pause it."
        )

    res.status = ReserveState.WAITING
    res.save()
    messages = []

    if res.callback:
        logger.info("Sending Transition Message")
        messages.append(
            (
                res.callback,
                ReserveTransitionMessage(
                    data={"state": ReserveState.WAITING, "message": message},
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
            {"action": ReserveState.WAITING.value, "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )

    return messages


def log_event_to_reservation(res: Reservation, event: Event):
    event_s = json.dumps(event.dict())

    prov_log = ReservationLog.objects.create(
        **{"reservation": res, "message": event_s, "level": LogLevel.EVENT}
    )

    ReservationEventSubscription.broadcast(
        {"action": "log", "data": {"message": event_s, "level": LogLevel.EVENT}},
        [f"reservation_{res.reference}"],
    )


def log_to_reservation(
    res: Reservation, message: str = "Critical", level=LogLevel.INFO
):
    prov_log = ReservationLog.objects.create(
        **{"reservation": res, "message": message, "level": level}
    )

    ReservationEventSubscription.broadcast(
        {"action": "log", "data": {"message": message, "level": level}},
        [f"reservation_{res.reference}"],
    )
