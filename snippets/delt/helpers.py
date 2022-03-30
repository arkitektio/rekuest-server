from facade.enums import ReservationStatus
from lok.bouncer.bounced import Bounced
from delt.messages import *
from lok.models import LokApp, LokUser
from facade.models import Reservation, Assignation, Provision
from facade.subscriptions import (
    MyAssignationsEvent,
    MyReservationsEvent,
)
import logging

logger = logging.getLogger(__name__)


def create_context_from_bounced(bounce: Bounced):
    return {
        "roles": bounce.roles,
        "scopes": bounce.scopes,
        "user": bounce.user.email if bounce.user else None,
        "app": bounce.app.client_id if bounce.app else None,
    }


def create_assignation_from_bouncedassign(bounced_assign: BouncedAssignMessage):
    context = bounced_assign.meta.context
    extensions = bounced_assign.meta.extensions

    ass = Assignation.objects.create(
        **{
            "reservation": Reservation.objects.get(
                reference=bounced_assign.data.reservation
            ),
            "args": bounced_assign.data.args,
            "kwargs": bounced_assign.data.kwargs,
            "context": context.dict(),
            "reference": bounced_assign.meta.reference,
            "creator": LokUser.objects.get(email=context.user),
            "app": LokApp.objects.get(client_id=context.app),
            "callback": extensions.callback,
            "progress": extensions.progress,
        }
    )

    MyAssignationsEvent.broadcast(
        {"action": "created", "data": ass.id}, [f"assignations_user_{context.user}"]
    )


def create_reservation_from_bouncedreserve(bounced_reserve: BouncedReserveMessage):
    context = bounced_reserve.meta.context
    extensions = bounced_reserve.meta.extensions

    res = Reservation.objects.create(
        **{
            "node_id": bounced_reserve.data.node,
            "template_id": bounced_reserve.data.template,
            "params": bounced_reserve.data.params.dict()
            if bounced_reserve.data.params
            else {},
            "context": context.dict(),
            "reference": bounced_reserve.meta.reference,
            "causing_provision": Provision.objects.get(
                reference=bounced_reserve.data.provision
            )
            if bounced_reserve.data.provision
            else None,
            "creator": LokUser.objects.get(email=context.user)
            if context.user
            else None,
            "app": LokApp.objects.get(client_id=context.app),
            "callback": extensions.callback,
            "progress": extensions.progress,
            "status": ReservationStatus.ROUTING,
        }
    )

    logger.info(f"Created Reservation {res}")

    if res.creator:
        MyReservationsEvent.broadcast(
            {"action": "created", "data": res.id},
            [f"reservations_user_{res.creator.id}"],
        )


async def create_bounced_assign_from_assign(
    assign: AssignMessage, bounce: Bounced, callback, progress
) -> BouncedAssignMessage:

    bounced_assign = BouncedAssignMessage(
        data=assign.data,
        meta={
            "reference": assign.meta.reference,
            "context": create_context_from_bounced(bounce),
            "extensions": {
                "callback": callback,
                "progress": progress,
            },
        },
    )

    return bounced_assign


async def create_bounced_unassign_from_unassign(
    unassign: UnassignMessage, bounce: Bounced, callback, progress
) -> BouncedUnassignMessage:

    bounced_cancel_assign = BouncedUnassignMessage(
        data=unassign.data,
        meta={
            "reference": unassign.meta.reference,
            "context": create_context_from_bounced(bounce),
            "extensions": {
                "callback": callback,
                "progress": progress,
            },
        },
    )

    return bounced_cancel_assign


async def create_bounced_reserve_from_reserve(
    reserve: ReserveMessage, bounce: Bounced, callback, progress
) -> BouncedReserveMessage:

    bounced = BouncedReserveMessage(
        data=reserve.data,
        meta={
            "reference": reserve.meta.reference,
            "extensions": {
                "callback": callback,
                "progress": progress,
            },
            "context": create_context_from_bounced(bounce),
        },
    )

    return bounced


async def create_bounced_unreserve_from_unreserve(
    unreserve: UnreserveMessage, bounce: Bounced, callback, progress
) -> BouncedUnreserveMessage:

    bounced = BouncedUnreserveMessage(
        data={
            "reservation": unreserve.data.reservation,
        },
        meta={
            "reference": unreserve.meta.reference,
            "extensions": {
                "callback": callback,
                "progress": progress,
            },
            "context": create_context_from_bounced(bounce),
        },
    )

    return bounced


def get_channel_for_reservation(reservation_reference: str) -> str:
    reservation = Reservation.objects.get(reference=reservation_reference)
    return f"assignments_in_{reservation.channel}"
