from delt.types import ReserveParams
from facade.enums import AssignationStatus, ProvisionStatus, ReservationStatus
from facade import models
from asgiref.sync import sync_to_async
from hare.carrots import (
    AssignHareMessage,
    AssignationChangedHareMessage,
    ReservationChangedMessage,
    ReserveHareMessage,
    UnreserveHareMessage,
)
from hare import messages
from hare.consumers.agent.protocols.agent_json import *
import logging
from arkitekt.console import console

logger = logging.getLogger(__name__)


@sync_to_async
def list_provisions(m: ProvisionList, agent: models.Agent, **kwargs):
    reply = []
    forward = []

    try:
        provisions = models.Provision.objects.filter(bound=agent,).exclude(
            status__in=[
                ProvisionStatus.CANCELLED,
                ProvisionStatus.CANCELING,
            ]
        )

        provisions = [
            messages.Provision(
                provision=prov.id, status=prov.status, template=prov.template.id
            )
            for prov in provisions
        ]

        reply += [ProvisionListReply(id=m.id, provisions=provisions)]
    except Exception as e:
        reply += [ProvisionListDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def list_assignations(m: AssignationsList, agent: models.Agent, **kwargs):
    reply = []
    forward = []
    print(agent.name)
    try:
        assignations = models.Assignation.objects.filter(
            provision__bound=agent
        ).exclude(
            status__in=[
                AssignationStatus.RETURNED,
                AssignationStatus.CANCELING,
                AssignationStatus.CANCELLED,
                AssignationStatus.ACKNOWLEDGED,
            ]
        )

        assignations = [
            messages.Assignation(
                assignation=ass.id,
                status=ass.status,
                args=ass.args,
                kwargs=ass.kwargs,
                reservation=ass.reservation.id,
                provision=ass.provision.id,
            )
            for ass in assignations
        ]

        print(assignations)

        reply += [AssignationsListReply(id=m.id, assignations=assignations)]
    except Exception as e:
        logger.exception(e)
        reply += [AssignationsListDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def bind_assignation(m: AssignHareMessage, prov: str, **kwargs):
    reply = []
    forward = []
    try:
        ass = models.Assignation.objects.get(id=m.assignation)
        ass.provision_id = prov
        ass.save()

        reply += [
            AssignSubMessage(
                assignation=ass.id,
                status=ass.status,
                args=ass.args,
                kwargs=ass.kwargs,
                reservation=ass.reservation.id,
                provision=ass.provision.id,
            )
        ]

    except Exception as e:
        logger.exception(e)

    return reply, forward


@sync_to_async
def change_assignation(m: AssignationChangedMessage, agent: models.Agent):
    reply = []
    forward = []
    try:
        logger.info("OOINAOISNOAISNOINEOINA, WE ARE HERE")
        ass = models.Assignation.objects.get(id=m.assignation)
        ass.status = m.status if m.status else ass.status
        ass.args = m.args if m.args else ass.args
        ass.kwargs = m.kwargs if m.kwargs else ass.kwargs
        ass.returns = m.returns if m.returns else ass.returns
        ass.statusmessage = m.message if m.message else ass.statusmessage
        ass.save()

        forward += [
            AssignationChangedHareMessage(
                queue=ass.reservation.waiter.queue,
                reservation=ass.reservation.id,
                provision=ass.provision.id,
                **m.dict(exclude={"provision", "reservation", "type"}),
            )
        ]

    except Exception as e:
        logger.exception(e)

    return reply, forward


@sync_to_async
def change_provision(m: ProvisionChangedMessage, agent: models.Agent):
    reply = []
    forward = []
    print("Chaning Provisions")
    try:
        provision = models.Provision.objects.get(id=m.provision)
        provision.status = m.status if m.status else provision.status
        provision.statusmessage = m.message if m.message else provision.statusmessage
        provision.mode = m.mode if m.mode else provision.mode  #
        provision.save()

        if provision.status == ProvisionStatus.CRITICAL:
            print("We are now Dead??")
            for res in provision.reservations.filter(status=ReservationStatus.ACTIVE):
                print("Found one?")
                res_params = ReserveParams(**res.params)
                viable_provisions_amount = min(
                    res_params.minimalInstances, res_params.desiredInstances
                )

                if (
                    res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                    <= viable_provisions_amount
                ):
                    res.status = ReservationStatus.DISCONNECT
                    res.save()
                    print("You are dead boy?")
                    forward += [
                        ReservationChangedMessage(
                            queue=res.waiter.queue,
                            reservation=res.id,
                            status=res.status,
                        )
                    ]

        if provision.status == ProvisionStatus.CANCELLED:
            print("We are now Dead??")
            for res in provision.reservations.filter(status=ReservationStatus.ACTIVE):
                print("Found one?")
                res_params = ReserveParams(**res.params)
                viable_provisions_amount = min(
                    res_params.minimalInstances, res_params.desiredInstances
                )

                if (
                    res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                    <= viable_provisions_amount
                ):
                    res.status = ReservationStatus.CANCELLED
                    res.save()
                    print("You are dead boy?")
                    forward += [
                        ReservationChangedMessage(
                            queue=res.waiter.queue,
                            reservation=res.id,
                            status=res.status,
                            message="We were cancelled because the provision was cancelled.",
                        )
                    ]

    except Exception as e:
        console.print_exception()

    return reply, forward


@sync_to_async
def accept_reservation(m: ReserveHareMessage, agent: models.Agent):
    """SHould accept a reserve Hare Message
    and if this reservation is viable cause it to get
    active"""
    reply = []
    forward = []
    reservation_queues = []
    try:
        res = models.Reservation.objects.get(id=m.reservation)
        viable_provisions_amount = ReserveParams(**res.params).minimalInstances

        if (
            res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
            >= viable_provisions_amount
        ):
            res.status = ReservationStatus.ACTIVE
            res.save()
            forward += [
                ReservationChangedMessage(
                    queue=res.waiter.queue,
                    reservation=res.id,
                    status=res.status,
                )
            ]

        reservation_queues += [(res.id, res.queue)]

    except Exception as e:
        console.print_exception()

    return reply, forward, reservation_queues


@sync_to_async
def loose_reservation(m: UnreserveHareMessage, agent: models.Agent):
    """SHould accept a reserve Hare Message
    and if this reservation is viable cause it to get
    active"""
    reply = []
    forward = []
    deleted_queues = []
    try:
        prov = models.Provision.objects.get(id=m.provision)
        res = models.Reservation.objects.get(id=m.reservation)
        prov.reservations.remove(res)

        if prov.reservations.count() == 0:

            reply += [
                UnprovideSubMessage(
                    provision=prov.id,
                    message=f"Was cancelled because last remaining reservation was cancelled {res}",
                )
            ]

        prov.save()
        deleted_queues += [res.id]

    except Exception as e:
        console.print_exception()

    return reply, forward, deleted_queues


@sync_to_async
def activate_provision(m: ProvisionChangedMessage, agent: models.Agent):
    reply = []
    forward = []
    reservation_queues = []
    assert m.status == ProvisionStatus.ACTIVE
    print("Chaning Provisions")
    try:
        provision = models.Provision.objects.get(id=m.provision)
        provision.status = m.status if m.status else provision.status
        provision.statusmessage = m.message if m.message else provision.statusmessage
        provision.mode = m.mode if m.mode else provision.mode  #
        provision.save()

        print("We are now active?")
        for res in provision.reservations.filter():
            print("Found one?")
            res_params = messages.ReserveParams(**res.params)
            viable_provisions_amount = min(
                res_params.minimalInstances or 1, res_params.desiredInstances or 1
            )

            if (
                res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                >= viable_provisions_amount
            ):
                res.status = ReservationStatus.ACTIVE
                res.save()
                print("Nanananan")
                forward += [
                    ReservationChangedMessage(
                        queue=res.waiter.queue,
                        reservation=res.id,
                        status=res.status,
                    )
                ]

            reservation_queues += [(res.id, res.queue)]

    except Exception as e:
        console.print_exception()

    return reply, forward, reservation_queues


@sync_to_async
def disconnect_agent(agent: models.Agent, close_code: int):
    forward = []
    print(agent.name)
    for provision in agent.bound_provisions.exclude(
        status__in=[ProvisionStatus.CANCELLED, ProvisionStatus.ENDED]
    ).all():
        provision.status = ProvisionStatus.DISCONNECTED
        provision.save()

        for res in provision.reservations.all():
            print("Found one?")
            res_params = ReserveParams(**res.params)
            viable_provisions_amount = min(
                res_params.minimalInstances, res_params.desiredInstances
            )

            if (
                res.provisions.filter(status=ProvisionStatus.ACTIVE).count()
                < viable_provisions_amount
            ):
                res.status = ReservationStatus.DISCONNECT
                res.save()
                forward += [
                    ReservationChangedMessage(
                        queue=res.waiter.queue,
                        reservation=res.id,
                        status=res.status,
                    )
                ]

    return forward
