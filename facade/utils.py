from delt.messages.postman.provide.provide_transition import ProvideState
from delt.messages.postman.reserve.reserve_transition import ReserveState
from delt.messages.postman.provide.provide_log import ProvideLogMessage
from delt.messages.postman.reserve.reserve_log import ReserveLogMessage
from typing import List, Tuple
from delt.messages import ReserveTransitionMessage, ProvideTransitionMessage
from delt.messages.base import MessageDataModel, MessageMetaModel, MessageModel
from facade.subscriptions.assignation import AssignationEventSubscription, MyAssignationsEvent
from facade.subscriptions.reservation import ReservationEventSubscription, MyReservationsEvent
from facade.subscriptions.provision import ProvisionEventSubscription, MyProvisionsEvent
from facade.enums import AssignationStatus, LogLevel, ProvisionStatus
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from asgiref.sync import sync_to_async
from .models import Assignation, AssignationLog, Agent, Provision, ProvisionLog, Reservation, ReservationLog, ReservationStatus
import logging

logger = logging.getLogger(__name__)


def log_to_reservation(reference: str, message: str, level=LogLevel.INFO, persist=True, callback=None) -> Tuple[str, MessageDataModel]:

    if persist:
        reservation_log = ReservationLog.objects.create(**{
            "reservation": Reservation.objects.get(reference=reference),
            "message": message,
            "level": level
        })


        ReservationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"reservation_{reference}"])

    if callback is not None:
            reserve_progress =  ReserveLogMessage(data={"level": level, "message": message}, meta={"reference": reference})
            return callback, reserve_progress

    return None, None


def log_to_provision(reference: str, message: str, level=LogLevel.INFO, persist=True, callback=None):

    if persist:
        provision_log = ProvisionLog.objects.create(**{
            "provision": Provision.objects.get(reference=reference),
            "message": message,
            "level": level
        })

        ProvisionEventSubscription.send_log([f"provision_{reference}"],message,level=level)

    if callback is not None:
        reserve_progress =  ProvideLogMessage(data={"level": level, "message": message}, meta={"reference": reference})
        return callback, reserve_progress

    return None, None



def transition_reservation_by_reference(reference: str, state: ReservationStatus, message: str = "Caused by a Transition"):
    provision = Reservation.objects.get(reference=reference)
    return transition_reservation(provision, state, message)
    

def transition_reservation(reservation: Reservation, state: ReservationStatus, message: str = "Caused by a Transition") -> List[Tuple[str, MessageModel]]:
    reservation.status = state
    reservation.save()

    messages = []
    if reservation.callback:
        messages.append((reservation.callback,  ReserveTransitionMessage(data= {
            "state": state,
            "message": message
        },meta = {
            "reference": reservation.reference,
        }
        )))

    if reservation:creator: MyReservationsEvent.broadcast({"action": state.value, "data": reservation.id}, [f"reservations_user_{reservation.creator.id}"])
    
    return messages



def transition_provision_by_reference(reference: str, state: ProvideState, message: str = "Transition"):
    provision = Provision.objects.get(reference=reference)
    return transition_provision(provision, state, message)



def transition_provision(reference: str, state: ProvideState, message: str = "Transition") -> List[Tuple[str, MessageModel]]:
    provision = Provision.objects.get(reference=reference)
    provision.status = state
    provision.save()

    messages = []
    if provision.callback:
        messages.append((provision.callback,  ReserveTransitionMessage(data= {
            "state": state,
            "message": message
        },meta = {
            "reference": provision.reference,
        }
        )))

    if state == ProvisionStatus.INACTIVE:
        for res in provision.reservations.all():
            if res.provisions.count() == 1:
                messages += transition_reservation(res, ReservationStatus.ERROR)


    if state == ProvisionStatus.ACTIVE:
        for res in provision.reservations.all():
            if res.status != ReservationStatus.ACTIVE:
                messages += transition_reservation(res, ReservationStatus.ACTIVE)


    if provision.creator: MyProvisionsEvent.broadcast({"action": state.value, "data": provision.id}, [f"provisions_user_{provision.creator.id}"])
    return messages




def create_assignation_from_bounced_assign(assign: BouncedAssignMessage):
    
    assignation, created = Assignation.objects.update_or_create(reference=assign.meta.reference, defaults={
        "reservation__reference": assign.data.reservation,
        "args": assign.data.args,
        "kwargs": assign.data.kwargs,
        "creator_id": assign.meta.token.user,
        "callback": assign.meta.extensions.callback,
        "progress": assign.meta.extensions.progress
    })

    # Signal Broadcasting
    MyAssignationsEvent.broadcast({"action": "created", "data": str(assignation.id)}, [f"assignations_user_{assign.meta.token.user}"])
    AssignationEventSubscription.broadcast({"action": "update", "data": str(assignation.id)}, [f"assignation_{assign.meta.reference}"])

    return assignation



def set_assignation_status(reference: str, status: AssignationStatus):
    assignation = Assignation.objects.get(reference=reference)
    assignation.status = status
    assignation.save()

    if assignation.creator: MyAssignationsEvent.broadcast({"action": status.value, "data": assignation.id}, [f"assignations_user_{assignation.creator.id}"])



def log_to_assignation(reference: str, message: str, level: LogLevel =LogLevel.INFO.value, persist=True):

    if persist:
        assignation_log = AssignationLog.objects.create(**{
            "reservation": Assignation.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    AssignationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"assignation_{reference}"])