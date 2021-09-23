from delt.messages.postman.log import LogLevel
from hare.transitions.reservation import activate_reservation, cancel_reservation, canceling_reservation, critical_reservation, disconnect_reservation
from facade.models import Provision, ProvisionLog
from delt.messages.postman.provide.provide_transition import ProvideState, ProvideTransistionData, ProvideTransitionMessage
from hare.transitions.base import TransitionException
from facade.subscriptions.provision import MyProvisionsEvent, ProvisionEvent, ProvisionEventSubscription
import logging

logger = logging.getLogger(__name__)

def log_to_provision_by_reference(reference, *args, **kwargs):
    prov = Provision.objects.get(reference=reference)
    return log_to_provision(prov, *args, **kwargs)

def activate_provision(prov: Provision, message: str = None):
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
    if prov.status == ProvideState.ACTIVE:
        # TODO: raise TransitionException(f"Provision {prov} was already active. Operation omitted to ensure Idempotence")
        pass

    if prov.status in [ProvideState.CANCELLED, ProvideState.ENDED]:
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")


    messages = []

    prov.status = ProvideState.ACTIVE
    prov.save()


    reservation_topic = f"reservations_in_{prov.unique}" # The channel we will use to listen to for Reservations
    assignment_topics = []

    for res in prov.reservations.all():
        try:
            logger.info(f"[green] Listening to {res}")
            res_messages, assignment_topic = activate_reservation(res, f"Activated by activated provision {prov}")
            assignment_topics.append(assignment_topic)
            messages += res_messages
        except TransitionException as e:
            logger.exception(e)

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.ACTIVE,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))

    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.ACTIVE.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages, reservation_topic, assignment_topics


def disconnect_provision(prov: Provision, message: str = None, reconnect = False):
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
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")


    prov.status = ProvideState.DISCONNECTED
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_messages = disconnect_reservation(res, message = f"Disconnected by disconnected provision {prov}", reconnect=reconnect)
            messages += res_messages
        except TransitionException as e:
            pass

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.DISCONNECTED,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))

    
    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.DISCONNECTED.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages

def providing_provision(prov: Provision, message: str = None, reconnect = False):
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
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")


    prov.status = ProvideState.PROVIDING
    prov.save()

    messages = []

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.PROVIDING,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))

    
    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.PROVIDING.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages

def cancel_provision(prov: Provision, message: str = None, reconnect = False):
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
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")


    prov.status = ProvideState.CANCELLED
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_messages = cancel_reservation(res, message = f"Cancelled through cancellation of provision {prov}", reconnect=reconnect)
            messages += res_messages
        except TransitionException as e:
            pass

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.DISCONNECTED,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))
    

    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.CANCELLED.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages

def cancelling_provision(prov: Provision, message: str = None, reconnect = False):
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
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")


    prov.status = ProvideState.CANCELING
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_messages = canceling_reservation(res, message = f"Cancelling through cancellation of provision {prov}", reconnect=reconnect)
            messages += res_messages
        except TransitionException as e:
            pass

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.CANCELING,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))
    

    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.CANCELING.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages


def critical_provision(prov: Provision, message: str = None, reconnect = False):
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
        raise TransitionException(f"Provision {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart.")

    prov.status = ProvideState.CRITICAL
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Criticalling {res}")
            res_messages = critical_reservation(res, message = f"Disconnected by disconnected provision {prov}", reconnect=reconnect)
            messages += res_messages
        except TransitionException as e:
            pass

    if prov.callback:
        messages.append((prov.callback,  ProvideTransitionMessage(data= {
            "state": ProvideState.CRITICAL,
            "message": message
        },meta = {
            "reference": prov.reference,
        }
        )))


    if prov.creator: MyProvisionsEvent.broadcast({"action": ProvideState.CRITICAL.value, "data": prov.id}, [f"provisions_user_{prov.creator.id}"])

    return messages

def log_to_provision(prov: Provision, message: str = "Critical", level=LogLevel.INFO):
    prov_log = ProvisionLog.objects.create(**{
        "provision": prov,
        "message": message,
        "level": level
    })

    ProvisionEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"provision_{prov.reference}"])