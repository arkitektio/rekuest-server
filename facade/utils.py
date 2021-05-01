from facade.subscriptions.assignation import AssignationEventSubscription
from facade.subscriptions.reservation import ReservationEventSubscription
from facade.enums import AssignationLogLevel, AssignationStatus, LogLevel, ReservationLogLevel
from facade.subscriptions.myassignations import MyAssignationsEvent
from delt.messages.postman.assign.bounced_assign import BouncedAssignMessage
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from facade.subscriptions.myreservations import MyReservationsEvent
from asgiref.sync import sync_to_async
from .models import Assignation, AssignationLog, Reservation, ReservationLog, ReservationStatus
import logging

logger = logging.getLogger(__name__)


@sync_to_async
def create_reservation_from_bounced_reserve(reserve: BouncedReserveMessage, persist=True):

    # We always persist a Reservation

    logger.info(reserve)

    reservation, updated = Reservation.objects.update_or_create(reference=reserve.meta.reference, defaults = {
        "node_id": reserve.data.node,
        "template_id": reserve.data.template,
        "status": ReservationStatus.PENDING.value,
        "params": reserve.data.params.dict(),
        "creator_id": reserve.meta.token.user,
        "callback": reserve.meta.extensions.callback,
        "progress": reserve.meta.extensions.progress
    })

    logger.info(reservation)
    # Signal Broadcasting
    MyReservationsEvent.broadcast({"action": "created", "data": reservation.id}, [f"reservations_user_{reserve.meta.token.user}"])
    ReservationEventSubscription.broadcast({"action": "updated", "data": reservation.id}, [f"reservation_{reservation.reference}"])

    return reservation

def update_reservation(reservation, node, template):
    # We always persist a Reservation
    reservation.node = node
    reservation.template = template
    reservation.save()    

    # Signal Broadcasting
    MyReservationsEvent.broadcast({"action": "updated", "data": reservation.id}, [f"reservations_user_{reservation.creator}"])
    ReservationEventSubscription.broadcast({"action": "updated", "data": reservation.id}, [f"reservation_{reservation.reference}"])

    return reservation


@sync_to_async
def update_reservation_with_ids(reference, node_id, template_id):

    # We always persist a Reservation

    logger.info(reference)
    reservation, updated = Reservation.objects.update_or_create(reference=reference, defaults = {
        "node_id": node_id,
        "template_id": template_id,
    })

    logger.info(reservation)
    # Signal Broadcasting
    MyReservationsEvent.broadcast({"action": "updated", "data": reservation.id}, [f"reservations_user_{reservation.creator}"])
    ReservationEventSubscription.broadcast({"action": "updated", "data": reservation.id}, [f"reservation_{reservation.reference}"])

    return reservation


@sync_to_async
def log_to_reservation(reference: str, message: str, level=ReservationLogLevel.INFO.value, persist=True):

    if persist:
        reservation_log = ReservationLog.objects.create(**{
            "reservation": Reservation.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    ReservationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"reservation_{reference}"])


@sync_to_async
def set_reservation_status(reference: str, status: ReservationStatus, persist=True):
    reservation = Reservation.objects.get(reference=reference)
    reservation.status = status
    reservation.save()

    MyReservationsEvent.broadcast({"action": "updated", "data": reservation.id}, [f"reservations_user_{reservation.creator.id}"])


@sync_to_async
def end_reservation(reference: str, persist=True):
    reservation = Reservation.objects.get(reference=reference)
    reservation.status = ReservationStatus.ENDED.value
    reservation.save()

    MyReservationsEvent.broadcast({"action": "ended", "data": str(reservation.id)}, [f"reservations_user_{reservation.creator.id}"])
    logger.warning(f"Reservation with id {reservation.id} ended")



@sync_to_async
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

@sync_to_async
def set_assignation_status(reference: str, status: AssignationStatus):
    assignation = Assignation.objects.get(reference=reference)
    assignation.status = status
    assignation.save()

    MyAssignationsEvent.broadcast({"action": "updated", "data": assignation.id}, [f"assignations_user_{assignation.creator.id}"])


@sync_to_async
def end_assignation(reference: str, persist=True, cancelled=False):
    assignation = Assignation.objects.get(reference=reference)
    assignation.status = AssignationStatus.DONE.value if not cancelled else AssignationStatus.CANCELLED.value
    assignation.save()

    MyAssignationsEvent.broadcast({"action": "ended", "data": str(assignation.id)}, [f"assignations_user_{assignation.creator.id}"])
    logger.warning(f"Assignation with id {assignation.id} ended")


@sync_to_async
def log_to_assignation(reference: str, message: str, level: LogLevel =LogLevel.INFO.value, persist=True):

    if persist:
        assignation_log = AssignationLog.objects.create(**{
            "reservation": Assignation.objects.get(reference=reference),
            "message": message,
            "level": level
        })

    AssignationEventSubscription.broadcast({"action": "log", "data": {"message": message, "level": level}}, [f"assignation_{reference}"])