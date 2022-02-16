from facade.enums import AssignationStatus, ReservationStatus
from facade.models import Assignation, Waiter, Reservation
from typing import Any, Dict, Optional
from asgiref.sync import async_to_sync, sync_to_async

from hare.consumers.messages import (
    AssignPub,
    AssignPubDenied,
    AssignPubReply,
    ReserveFragment,
    ReserveList,
    ReserveListDenied,
    ReserveListReply,
    ReservePub,
    ReservePubDenied,
    ReservePubReply,
    UnassignPub,
    UnassignPubReply,
    UnreservePub,
    UnreservePubReply,
)


@sync_to_async
def reserve(m: ReservePub, waiter: Waiter, **kwargs):

    try:
        res, created = Reservation.objects.get_or_create(
            node_id=m.node,
            creator=waiter.registry.user,
            params=m.params,
            app=waiter.registry.app,
            waiter=waiter,
            defaults={
                "status": ReservationStatus.ROUTING,
                "title": m.title,
                "callback": "not-set",
                "progress": "not-set",
            },
        )

        reply = ReservePubReply(id=m.id, reservation=res.id, status=res.status)
    except Exception as e:
        reply = ReservePubDenied(id=m.id, error=str(e))

    return [reply], [] if not created else ["7"]


@sync_to_async
def list_reservations(m: ReserveList, waiter: Waiter, **kwargs):

    try:
        reservations = Reservation.objects.filter(
            waiter=waiter,
        )

        reservations = [
            ReserveFragment(id=res.id, status=res.status) for res in reservations
        ]

        reply = ReserveListReply(id=m.id, reservations=reservations)
    except Exception as e:
        reply = ReserveListDenied(id=m.id, error=str(e))

    return [reply], []


@sync_to_async
def assign(m: AssignPub, waiter: Waiter, **kwargs):
    try:
        ass = Assignation.objects.create(
            **{
                "reservation_id": m.reservation,
                "args": m.args,
                "kwargs": m.kwargs,
                "waiter": waiter,
                "creator": waiter.registry.user,
                "app": waiter.registry.app,
                "status": AssignationStatus.ASSIGNED,
            }
        )

        reply = AssignPubReply(id=m.id, assignation=ass.id, status=ass.status)
    except Exception as e:
        reply = AssignPubDenied(id=m.id, error=repr(e))

    return [reply], []


@sync_to_async
def unassign(m: UnassignPub, waiter: Waiter, **kwargs):

    ass = Assignation.objects.get(id=m.assignation)
    ass.status = AssignationStatus.CANCELING
    ass.save()

    reply = UnassignPubReply(id=m.id, assignation=ass.id, status=ass.status)

    return [reply], []


@sync_to_async
def unreserve(m: UnreservePub, waiter: Waiter, **kwargs):

    res = Reservation.objects.get(id=m.reservation)
    res.status = ReservationStatus.CANCELING
    res.save()

    reply = UnreservePubReply(id=m.id, reservation=res.id, status=res.status)

    return [reply], []
