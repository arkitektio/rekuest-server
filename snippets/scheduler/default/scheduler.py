from typing import List
from delt.messages.postman.log import LogLevel
from delt.messages.postman.reserve.reserve_log import ReserveLogMessage
from delt.messages.postman.reserve.reserve_transition import (
    ReserveState,
    ReserveTransitionMessage,
)
from delt.messages.postman.unprovide.bounced_unprovide import BouncedUnprovideMessage
from delt.types import ProvideParams, ReserveParams
from facade.enums import ProvisionStatus, ReservationStatus
from facade.models import Reservation
from hare.carrots import RouteHareMessage, UnrouteHareMessage
from hare.scheduler.base import BaseScheduler, MessageEvent, SchedulerError
import logging

from .utils import (
    build_provision_escalation_qs,
    build_reserve_escalation_qs,
    escalate_through_provision_qs,
    escalate_through_reserve_qs,
    predicate_fullfiled,
    predicate_unprovide,
)

logger = logging.getLogger(__name__)


class DefaultScheduler(BaseScheduler):
    def _reserve_reservation(self, reservation: Reservation):

        params = ReserveParams(**reservation.params)
        all_events = []

        reserve_es_qs = build_reserve_escalation_qs(reservation, params)
        mapped_provisions, events = escalate_through_reserve_qs(
            reserve_es_qs, reservation, params, mapped_provisions={}
        )
        all_events += events

        if predicate_fullfiled(reservation, params, mapped_provisions):
            # No need to step through potential provisions
            return all_events

        logger.info("Trying to provide some")

        provide_es_qs = build_provision_escalation_qs(reservation, params)

        mapped_provisions, events = escalate_through_provision_qs(
            provide_es_qs,
            reservation,
            params,
            mapped_provisions=mapped_provisions,
        )
        all_events += events

        logger.info(f"New Events for Res {reservation} {all_events}")
        if len(mapped_provisions.items()) == 0:
            raise SchedulerError("Could not reserve one single Provision")

        return all_events

    def on_route(self, route: RouteHareMessage) -> List[MessageEvent]:

        reservation = Reservation.objects.get(reference=route.reservation)
        return self._reserve_reservation(reservation)

    def on_unroute(self, unroute: UnrouteHareMessage) -> List[MessageEvent]:
        events = []

        reservation = Reservation.objects.get(reference=unroute.reservation)
        params = ReserveParams(**reservation.params)

        needs_action = (
            False  # If we need to send any events to an agent this is the time
        )

        for prov in reservation.provisions.all():
            logger.info(f"ANANANAA {prov}")
            if prov.status == ProvisionStatus.ACTIVE:
                # Again we are on the active part
                needs_action = True
                reserve_log = ReserveLogMessage(
                    data={
                        "level": LogLevel.INFO,
                        "message": "Provision is Active so we send the Unassignment to the Agent for it to unreserve",
                    },
                    meta={"reference": reservation.reference},
                )

                events.append(MessageEvent(reservation.callback, reserve_log))
                events.append(MessageEvent(f"reservations_in_{prov.unique}", unreserve))

                if params.autoUnprovide:
                    logger.info("Unprovide was set to True")
                    if predicate_unprovide(reservation, prov):
                        reserve_log = ReserveLogMessage(
                            data={
                                "level": LogLevel.INFO,
                                "message": f"Provision {prov} has no other reservations attached and autoUnprovide was set to True. So we are cancelling it through sending a request to the Agent",
                            },
                            meta={"reference": reservation.reference},
                        )

                        un_provide = BouncedUnprovideMessage(
                            data={
                                "provision": prov.reference,
                            },
                            meta={"reference": reservation.reference},
                        )

                        events.append(MessageEvent(reservation.callback, reserve_log))
                        events.append(
                            MessageEvent(
                                f"provision_agent_in_{prov.bound.unique}", un_provide
                            )
                        )

            else:
                events = []

                params = ProvideParams(**prov.params)
                prov.reservations.remove(reservation)

                reserve_log = ReserveLogMessage(
                    data={
                        "level": LogLevel.INFO,
                        "message": f"We removed the Reservation {reservation.reference} from {prov.reference}",
                    },
                    meta={"reference": reservation.reference},
                )

                events.append(MessageEvent(reservation.callback, reserve_log))

                if params.autoUnprovide:
                    logger.info(
                        "Autounprovide  was set to True. Checking if needs to unprovide"
                    )
                    if predicate_unprovide(reservation, prov):
                        prov.status = ProvisionStatus.CANCELLED
                        prov.save()

                        reserve_log = ReserveLogMessage(
                            data={
                                "level": LogLevel.INFO,
                                "message": f"Provision {prov} has no other reservations attached and autoUnprovide was set to True. Also Agent is offline. Voila it is gone!",
                            },
                            meta={"reference": reservation.reference},
                        )

                        events.append(MessageEvent(reservation.callback, reserve_log))

        if not needs_action:

            reserve_log = ReserveLogMessage(
                data={
                    "level": LogLevel.INFO,
                    "message": "None of the provisions are active. We are just setting this Reservation offline",
                },
                meta={"reference": reservation.reference},
            )

            reservation.status = ReservationStatus.CANCELLED
            reservation.save()

            reserve_transition = ReserveTransitionMessage(
                data={
                    "state": ReserveState.CANCELLED,
                    "message": "Provision was not active so we just remove the reservation",
                },
                meta={"reference": reservation.reference},
            )

            events.append(MessageEvent(reservation.callback, reserve_log))
            events.append(MessageEvent(reservation.callback, reserve_transition))

        return events
