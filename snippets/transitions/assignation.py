from facade.enums import AssignationStatus, LogLevel
from typing import List
from facade.models import Assignation, AssignationLog, Provision
from hare.transitions.base import TransitionException
from facade.subscriptions.assignation import (
    MyAssignationsEvent,
    AssignationEventSubscription,
)

# Big Difference here is that we need not to keep any state (insted of reservation so
# all of the messages cann be handled outside)


def yield_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return yield_assignation(ass, *args, **kwargs)


def receive_assignation_by_reference(reference, provision, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    prov = Provision.objects.get(reference=provision)
    return receive_assignation(ass, prov, *args, **kwargs)


def done_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return done_assignation(ass, *args, **kwargs)


def cancel_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return cancel_assignation(ass, *args, **kwargs)


def critical_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return critical_assignation(ass, *args, **kwargs)


def return_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return return_assignation(ass, *args, **kwargs)


def log_to_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return log_to_assignation(ass, *args, **kwargs)


def log_to_assignation(
    ass: Assignation, message: str = "Critical", level=LogLevel.INFO
):
    assignation_log = AssignationLog.objects.create(
        **{"assignation": ass, "message": message, "level": level}
    )

    AssignationEventSubscription.broadcast(
        {"action": "log", "data": {"message": message, "level": level}},
        [f"assignation_{ass.reference}"],
    )


def yield_assignation(ass: Assignation, returns: List, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(
            f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.YIELD
    ass.returns = returns
    ass.save()

    log_to_assignation(ass, message=message, level=LogLevel.YIELD)
    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.YIELD.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )


def done_assignation(ass: Assignation, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(
            f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.DONE
    ass.returns = []
    ass.save()

    log_to_assignation(ass, message=message, level=LogLevel.DONE)
    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.DONE.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )


def cancel_assignation(ass: Assignation, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(
            f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.CANCELLED
    ass.returns = []
    ass.save()

    log_to_assignation(ass, message=message, level=LogLevel.CANCEL)
    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.CANCELLED.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )


def receive_assignation(ass: Assignation, provision: Provision):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(
            f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.RECEIVED
    ass.provision = provision
    ass.save()

    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.RECEIVED.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )


def return_assignation(ass: Assignation, returns: List, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(
            f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.RETURNED
    ass.returns = returns
    ass.save()

    log_to_assignation(ass, message=message, level=LogLevel.RETURN)
    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.RETURNED.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )


def critical_assignation(ass: Assignation, message: str = "Critical"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.CRITICAL]:
        raise TransitionException(
            f"Assignation {ass} was already ended or criticalled. Operation omitted. Create a new Assignation if you want to yield to it."
        )

    ass.status = AssignationStatus.CRITICAL
    ass.save()

    log_to_assignation(ass, message=message, level=LogLevel.CRITICAL)
    # Signal Broadcasting
    if ass.creator:
        MyAssignationsEvent.broadcast(
            {"action": AssignationStatus.CRITICAL.value, "data": str(ass.id)},
            [f"assignations_user_{ass.creator.id}"],
        )
