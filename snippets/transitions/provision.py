from delt.events.base import Event
from delt.events import ProvisionTransitionEvent
from delt.messages.postman.log import LogLevel
from delt.types import ReserveParams
from facade.enums import ProvisionStatus
from hare.transitions.reservation import (
    activate_reservation,
    cancel_reservation,
    canceling_reservation,
    critical_reservation,
    disconnect_reservation,
    log_event_to_reservation,
)
from facade.models import Provision, ProvisionLog
from delt.messages.postman.provide.provide_transition import (
    ProvideState,
    ProvideTransitionMessage,
)
from hare.transitions.base import TransitionException
from facade.subscriptions.provision import (
    MyProvisionsEvent,
    ProvisionEventSubscription,
)
import logging
import json

logger = logging.getLogger(__name__)


def log_to_provision_by_reference(reference, *args, **kwargs):
    prov = Provision.objects.get(reference=reference)
    return log_to_provision(prov, *args, **kwargs)


def activate_provision(prov: Provision, message: ProvideTransitionMessage):
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
        raise TransitionException(
            f"Provisions {prov} was already ended or cancelled. Operation omitted. Create a new Provision if you want to restart."
        )

    messages = []
    prov.status = ProvideState.ACTIVE
    prov.mode = message.data.mode
    prov.statusmessage = message.data.message
    prov.save()

    reservation_topic = f"reservations_in_{prov.unique}"  # The channel we will use to listen to for Reservations
    assignment_topics = []

    for res in prov.reservations.all():
        try:
            logger.info(f"[green] Listening to {res}")

            res_params = ReserveParams(**res.params)
            print(res_params)
            viable_provisions_amount = min(
                res_params.minimalInstances, res_params.desiredInstances
            )

            logger.error(f"MINIMAL VIABALE AMOUNT {viable_provisions_amount}")

            if (
                res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                >= viable_provisions_amount
            ):
                messages += activate_reservation(
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

    if prov.callback:
        messages.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": "updated", "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages, reservation_topic, assignment_topics


def disconnect_provision(prov: Provision, message: ProvideTransitionMessage):
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

    prov.status = ProvideState.DISCONNECTED
    prov.statusmessage = message.data.message
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_params = ReserveParams(**res.params)
            viable_provisions_amount = min(
                res_params.minimalInstances, res_params.desiredInstances
            )
            if (
                res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                < viable_provisions_amount
            ):
                messages += disconnect_reservation(
                    res, message=f"Disconnected by disconnected provision {prov}"
                )

            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
        except TransitionException:
            pass

    if prov.callback:
        messages.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.DISCONNECTED.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages


def providing_provision(prov: Provision, message: ProvideTransitionMessage):
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

    prov.status = ProvideState.PROVIDING
    prov.statusmessage = message.data.message
    prov.save()

    messages = []

    if prov.callback:
        messages.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.PROVIDING.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages


def cancel_provision(prov: Provision, message: ProvideTransitionMessage):
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
    prov.statusmessage = message.data.message
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_messages = cancel_reservation(
                res, message=f"Cancelled through cancellation of provision {prov}"
            )
            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
            messages += res_messages
        except TransitionException:
            pass

    if prov.callback:
        messages.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.CANCELLED.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages


def cancelling_provision(prov: Provision, message: ProvideTransitionMessage):
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

    prov.status = ProvideState.CANCELING
    prov.statusmessage = message.data.message
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Disconnecting {res}")
            res_messages = canceling_reservation(
                res, message=f"Cancelling through cancellation of provision {prov}"
            )
            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
            messages += res_messages
        except TransitionException:
            pass

    if prov.callback:
        messages.append(
            (
                prov.callback,
                ProvideTransitionMessage(
                    data={"state": ProvideState.CANCELING, "message": message},
                    meta={
                        "reference": prov.reference,
                    },
                ),
            )
        )

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.CANCELING.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages


def critical_provision(prov: Provision, message: ProvideTransitionMessage):
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
    prov.statusmessage = message.data.message
    prov.save()

    messages = []

    for res in prov.reservations.all():
        try:
            logger.info(f"Criticalling {res}")
            res_messages = critical_reservation(
                res, message=f"Disconnected by disconnected provision {prov}"
            )
            log_event_to_reservation(
                res,
                ProvisionTransitionEvent(provision=prov.reference, state=prov.status),
            )
            messages += res_messages
        except TransitionException:
            pass

    if prov.callback:
        messages.append((prov.callback, message))

    if prov.creator:
        MyProvisionsEvent.broadcast(
            {"action": ProvideState.CRITICAL.value, "data": prov.id},
            [f"provisions_user_{prov.creator.id}"],
        )

    return messages


def log_event_to_provision(prov: Provision, event: Event):
    prov_log = ProvisionLog.objects.create(
        **{"provision": prov, "message": json.dumps(event), "level": LogLevel.EVENT}
    )

    ProvisionEventSubscription.broadcast(
        {
            "action": "log",
            "data": {"message": json.dumps(event), "level": LogLevel.EVENT},
        },
        [f"provision_{prov.reference}"],
    )


def log_to_provision(prov: Provision, message: str = "Critical", level=LogLevel.INFO):
    prov_log = ProvisionLog.objects.create(
        **{"provision": prov, "message": message, "level": level}
    )

    ProvisionEventSubscription.broadcast(
        {"action": "log", "data": {"message": message, "level": level}},
        [f"provision_{prov.reference}"],
    )
