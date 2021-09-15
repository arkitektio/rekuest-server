from facade.enums import AssignationStatus
from typing import List
from facade.models import Assignation
from hare.transitions.base import TransitionException
from facade.subscriptions.assignation import MyAssignationsEvent, AssignationEventSubscription
# Big Difference here is that we need not to keep any state (insted of reservation so
# all of the messages cann be handled outside)

def yield_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return yield_assignation(ass, *args, **kwargs)

def done_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return done_assignation(ass, *args, **kwargs)

def cancel_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return cancel_assignation(ass, *args, **kwargs)

def return_assignation_by_reference(reference, *args, **kwargs):
    ass = Assignation.objects.get(reference=reference)
    return return_assignation(ass, *args, **kwargs)


def yield_assignation(ass: Assignation, returns: List, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it.")
    
    ass.status = AssignationStatus.YIELD
    ass.returns = returns
    ass.save()

    # Signal Broadcasting
    if ass.creator: MyAssignationsEvent.broadcast({"action": AssignationStatus.YIELD.value, "data": str(ass.id)}, [f"assignations_user_{ass.creator.id}"])
    AssignationEventSubscription.broadcast({"action": "update", "data": str(ass.id)}, [f"assignation_{ass.reference}"])

def done_assignation(ass: Assignation, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it.")
    
    ass.status = AssignationStatus.DONE
    ass.returns = []
    ass.save()

    # Signal Broadcasting
    if ass.creator: MyAssignationsEvent.broadcast({"action": AssignationStatus.DONE.value, "data": str(ass.id)}, [f"assignations_user_{ass.creator.id}"])
    AssignationEventSubscription.broadcast({"action": "update", "data": str(ass.id)}, [f"assignation_{ass.reference}"])

def cancel_assignation(ass: Assignation, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it.")
    
    ass.status = AssignationStatus.CANCELLED
    ass.returns = []
    ass.save()

    # Signal Broadcasting
    if ass.creator: MyAssignationsEvent.broadcast({"action": AssignationStatus.CANCELLED.value, "data": str(ass.id)}, [f"assignations_user_{ass.creator.id}"])
    AssignationEventSubscription.broadcast({"action": "update", "data": str(ass.id)}, [f"assignation_{ass.reference}"])

def return_assignation(ass: Assignation, returns: List, message: str = "Yielded"):
    if ass.status in [AssignationStatus.CANCELLED, AssignationStatus.DONE]:
        raise TransitionException(f"Assignation {ass} was already ended or cancelled. Operation omitted. Create a new Assignation if you want to yield to it.")
    
    ass.status = AssignationStatus.RETURNED
    ass.returns = returns
    ass.save()

    # Signal Broadcasting
    if ass.creator: MyAssignationsEvent.broadcast({"action": AssignationStatus.RETURNED.value, "data": str(ass.id)}, [f"assignations_user_{ass.creator.id}"])
    AssignationEventSubscription.broadcast({"action": "update", "data": str(ass.id)}, [f"assignation_{ass.reference}"])
