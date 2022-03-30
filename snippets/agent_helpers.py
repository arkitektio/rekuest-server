import queue
from delt.types import ReserveParams
from facade.enums import AssignationStatus, ProvisionStatus, ReservationStatus
from facade.models import Agent, Assignation, Provision, Waiter, Reservation
from typing import Any, Dict, Optional
from asgiref.sync import async_to_sync, sync_to_async
from hare.consumers.carrots import AssignHareMessage, ReservationChangedMessage

from hare.consumers.agent_message import (
    AssignationsList,
    AssignationsListDenied,
    AssignationsListReply,
    ProvideFragment,
    ProvisionChangedMessage,
    ProvisionList,
    ProvisionListDenied,
    ProvisionListReply,
    AssignationFragment,
)
import logging

logger = logging.getLogger(__name__)


@sync_to_async
def list_provisions(m: ProvisionList, agent: Agent, **kwargs):
    reply = []
    forward = []

    try:
        provisions = Provision.objects.filter(
            bound=agent,
        )

        provisions = [
            ProvideFragment(
                provision=prov.id, status=prov.status, template=prov.template.id
            )
            for prov in provisions
        ]

        reply += [ProvisionListReply(id=m.id, provisions=provisions)]
    except Exception as e:
        reply += [ProvisionListDenied(id=m.id, error=str(e))]

    return reply, forward


@sync_to_async
def list_assignations(m: AssignationsList, agent: Agent, **kwargs):
    reply = []
    forward = []
    print(agent.name)
    try:
        assignations = Assignation.objects.filter(
            provision__bound=agent,
        )
        for assignation in assignations:
            print(assignation.args)
            print(assignation.kwargs)

        assignations = [
            AssignationFragment(
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
def change_provision(m: ProvisionChangedMessage, agent: Agent):
    reply = []
    forward = []
    print(agent.name)
    try:
        provision = Provision.objects.get(id=m.provision)
        provision.status = m.status if m.status else provision.status
        provision.statusmessage = m.message if m.message else provision.statusmessage
        provision.mode = m.mode if m.mode else provision.mode  #
        provision.save()

        if provision.status == ProvisionStatus.ACTIVE:
            print("We are now active?")
            for res in provision.reservations.filter(
                status=ReservationStatus.DISCONNECT
            ):
                print("Found one?")
                res_params = ReserveParams(**res.params)
                viable_provisions_amount = min(
                    res_params.minimalInstances, res_params.desiredInstances
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

    except Exception as e:
        logger.exception(e)

    return reply, forward
