
from facade.subscriptions.reservation import MyReservationsEvent
from delt.messages.postman.reserve.reserve_transition import ReserveState, ReserveTransitionMessage
from hare.transitions.base import TransitionException
from facade.models import Reservation

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
        #raise TransitionException(f"Reservation {res} was already active. Operation omitted to ensure Idempotence")
        pass
    
    if res.status in [ReserveState.CANCELLED, ReserveState.ENDED]:
        raise TransitionException(f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")

    res.status = ReserveState.ACTIVE
    res.save()
    messages = []

    if res.callback:
        messages.append((res.callback,  ReserveTransitionMessage(data= {
            "state": ReserveState.ACTIVE,
            "message": message
        },meta = {
            "reference": res.reference,
        }
        )))

        if res.creator: MyReservationsEvent.broadcast({"action": ReserveState.ACTIVE.value, "data": res.id}, [f"reservations_user_{res.creator.id}"])

    assignment_topic = f"assignments_in_{res.channel}"

    return messages, assignment_topic


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
        raise TransitionException(f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")

    res.status = ReserveState.DISCONNECT
    res.save()
    messages = []

    if res.callback:
        messages.append((res.callback,  ReserveTransitionMessage(data= {
            "state": ReserveState.DISCONNECT,
            "message": message
        },meta = {
            "reference": res.reference,
        }
        )))

        if reconnect:
            logger.error("Reconnection not implemented yet")
            #TODO: Implement a Reconnecting algorithm that tries to find another assignable node (through reserver)

        if res.creator: MyReservationsEvent.broadcast({"action": ReserveState.DISCONNECT.value, "data": res.id}, [f"reservations_user_{res.creator.id}"])

    return messages

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
        raise TransitionException(f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")

    res.status = ReserveState.CRITICAL
    res.save()
    messages = []

    if res.callback:
        messages.append((res.callback,  ReserveTransitionMessage(data= {
            "state": ReserveState.CRITICAL,
            "message": message
        },meta = {
            "reference": res.reference,
        }
        )))

        if reconnect:
            logger.error("Reconnection not implemented yet")
            #TODO: Implement a Reconnecting algorithm that tries to find another assignable node (through reserver)

        if res.creator: MyReservationsEvent.broadcast({"action": ReserveState.CRITICAL.value, "data": res.id}, [f"reservations_user_{res.creator.id}"])

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
        raise TransitionException(f"Reservation {res} was already ended or cancelled. Operation omitted. Create a new Provision if you want to cancel it.")

    res.status = ReserveState.CANCELLED
    res.save()
    messages = []

    if res.callback:
        messages.append((res.callback,  ReserveTransitionMessage(data= {
            "state": ReserveState.CANCELLED,
            "message": message
        },meta = {
            "reference": res.reference,
        }
        )))

        if reconnect:
            logger.error("Reconnection not implemented yet")
            #TODO: Implement a Reconnecting algorithm that tries to find another assignable node (through reserver)

        if res.creator: MyReservationsEvent.broadcast({"action": ReserveState.CANCELLED.value, "data": res.id}, [f"reservations_user_{res.creator.id}"])

    return messages
