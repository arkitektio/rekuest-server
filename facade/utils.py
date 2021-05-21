from facade.subscriptions.assignation import AssignationEventSubscription, MyAssignationsEvent
from facade.subscriptions.reservation import ReservationEventSubscription, MyReservationsEvent
from facade.subscriptions.provision import ProvisionEventSubscription, MyProvisionsEvent
from facade.enums import AssignationStatus, LogLevel, ProvisionStatus
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from asgiref.sync import sync_to_async
from .models import Assignation, AssignationLog, Provision, ProvisionLog, Reservation, ReservationLog, ReservationStatus
import logging

logger = logging.getLogger(__name__)


def log_to_reservation(reference: str, message: str, level=LogLevel.INFO, persist=True):

    if persist:
        reservation_log = ReservationLog.objects.create(**{
            "reservation": Reservation.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    ReservationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"reservation_{reference}"])


def log_to_provision(reference: str, message: str, level=LogLevel.INFO, persist=True):

    if persist:
        provision_log = ProvisionLog.objects.create(**{
            "provision": Provision.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    ProvisionEventSubscription.send_log([f"provision_{reference}"],message,level=level)


def set_provision_status(reference: str, status: ProvisionStatus, persist=True):
    provision = Provision.objects.get(reference=reference)
    provision.status = status
    provision.save()

    MyProvisionsEvent.broadcast({"action": status.value, "data": provision.id}, [f"provisions_user_{provision.creator.id}"])


def set_reservation_status(reference: str, status: ReservationStatus, persist=True):
    reservation = Reservation.objects.get(reference=reference)
    reservation.status = status
    reservation.save()

    MyReservationsEvent.broadcast({"action": status.value, "data": reservation.id}, [f"reservations_user_{reservation.creator.id}"])


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

    MyAssignationsEvent.broadcast({"action": status.value, "data": assignation.id}, [f"assignations_user_{assignation.creator.id}"])



def log_to_assignation(reference: str, message: str, level: LogLevel =LogLevel.INFO.value, persist=True):

    if persist:
        assignation_log = AssignationLog.objects.create(**{
            "reservation": Assignation.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    AssignationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"assignation_{reference}"])