from facade.enums import AssignationStatus, ReservationStatus
from facade import models
from typing import Any, Dict, Optional
from asgiref.sync import async_to_sync, sync_to_async
from hare.consumers.postman.protocols.postman_json import *
from hare.carrots import *
from arkitekt.console import console

@sync_to_async
def reserve(m: ReservePub, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:
        try:
            res = models.Reservation.objects.get(node_id=m.node, params=m.params.dict(), waiter=waiter)

            reply = [ReservePubReply(
                id=m.id,
                reservation=res.id,
                status=res.status,
                template=res.template.id  if res.template else None       
                )]


        except models.Reservation.DoesNotExist:

            res, forward = models.Reservation.objects.schedule(
                node = m.node,
                params = m.params,
                waiter = waiter,
                title = m.title
            )

            reply = [ReservePubReply(id=m.id,
                reservation=res.id,
                status=res.status,
                template=res.template.id if res.template else None )]
            forward = [RouteHareMessage(reservation=res.id)]

    except Exception as e:
        console.print_exception()
        reply += [ReservePubDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def list_reservations(m: ReserveList, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:
        reservations = models.Reservation.objects.filter(
            waiter=waiter,
        )

        reservations = [
            Reservation(reservation=res.id, status=res.status) for res in reservations
        ]

        reply += [ReserveListReply(id=m.id, reservations=reservations)]
    except Exception as e:
        reply += [ReserveListDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def list_assignations(m: AssignList, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:
        assignations = models.Assignation.objects.filter(
            waiter=waiter,
        )

        assignations = [
            Assignation(assignation=ass.id, status=ass.status) for ass in assignations
        ]

        reply += [AssingListReply(id=m.id, assignations=assignations)]
    except Exception as e:
        reply += [AssignListDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def assign(m: AssignPub, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:
        ass = models.Assignation.objects.create(
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

        reply += [AssignPubReply(id=m.id, assignation=ass.id, status=ass.status)]
        forward = [
            AssignHareMessage(
                queue=ass.reservation.queue,
                reservation=ass.reservation.id,
                assignation=ass.id,
                args=m.args,
                kwargs=m.kwargs,
                log=m.log,
            )
        ]
    except Exception as e:
        reply += [AssignPubDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def unassign(m: UnassignPub, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:

        ass = models.Assignation.objects.get(id=m.assignation)
        ass.status = AssignationStatus.CANCELING
        ass.save()

        reply += [UnassignPubReply(id=m.id, assignation=ass.id, status=ass.status)]

    except Exception as e:
        reply += [UnassignPubDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def unreserve(m: UnreservePub, waiter: models.Waiter, **kwargs):
    reply = []
    forward = []

    try:
        res = models.Reservation.objects.get(id=m.reservation)
        assert res.status not in [
            ReservationStatus.ENDED,
            ReservationStatus.CANCELLED,
        ], "Reservation was already unreserved before"

        res.status = ReservationStatus.CANCELING
        res.save()

        reply += [UnreservePubReply(id=m.id, reservation=res.id, status=res.status)]

    except Exception as e:
        reply += [UnreservePubDenied(id=m.id, error=str(e))]

    return reply, forward
