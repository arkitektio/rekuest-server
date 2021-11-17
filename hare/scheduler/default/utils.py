from typing import Dict, List, Tuple
from uuid import uuid4
from django.db.models.query import QuerySet
from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from delt.messages.postman.provide.provide_transition import ProvideState
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from delt.messages.postman.reserve.reserve_log import ReserveLogMessage
from facade.models import Provision, Reservation, Template
from delt.types import ProvideTactic, ReserveParams, ReserveTactic, TemplateParams
from django.db.models import Count
from facade.enums import AgentStatus, LogLevel, ProvisionStatus
from facade.subscriptions.provision import MyProvisionsEvent
from hare.scheduler.base import MessageEvent, SchedulerError
import logging


logger = logging.getLogger(__name__)


def build_reserve_escalation_qs(reservation: Reservation, params: ReserveParams):

    if not reservation.node and not reservation.template:
        raise SchedulerError("No Node or Reservation Provided")
    if reservation.node and not reservation.template:
        provisions_qs = Provision.objects.filter(
            template__node=reservation.node
        ).exclude(status=ProvisionStatus.CANCELLED)
    if reservation.template:
        provisions_qs = Provision.objects.filter(template=reservation.template).exclude(
            status=ProvisionStatus.CANCELLED
        )

    escalation_reservation_qs = []

    for index, el in enumerate(params.reserveStrategy):
        qs = escalation_reservation_qs[index - 1] if index != 0 else provisions_qs

        if el == ReserveTactic.ALL:
            escalation_reservation_qs.append(qs.all())

        if el == ReserveTactic.FILTER_TEMPLATES:
            if not params.templates or len(params.templates) == 0:
                escalation_reservation_qs.append(qs.all())
            else:
                escalation_reservation_qs.append(
                    qs.filter(template__pk__in=params.templates)
                )

        if el == ReserveTactic.FILTER_AGENTS:
            if not params.agents or len(params.agents) == 0:
                escalation_reservation_qs.append(qs.all())
            else:
                escalation_reservation_qs.append(
                    qs.filter(agents__pk__in=params.agents)
                )

        if el == ReserveTactic.FILTER_ACTIVE:
            escalation_reservation_qs.append(
                qs.filter(status__in=[ProvisionStatus.ACTIVE])
            )
        if el == ReserveTactic.FILTER_OWN:
            escalation_reservation_qs.append(qs.filter(creator=reservation.creator))

        if el == ReserveTactic.BALANCE:
            escalation_reservation_qs.append(
                qs.annotate(len_reservations=Count("reservations")).order_by(
                    "len_reservations"
                )
            )

    return escalation_reservation_qs


def build_provision_escalation_qs(reservation: Reservation, params: ReserveParams):

    if not reservation.node and not reservation.template:
        raise SchedulerError("No Node or Reservation Provided")
    if reservation.node and not reservation.template:
        template_qs = Template.objects.filter(node=reservation.node)
    if reservation.template:
        template_qs = Template.objects.filter(pk=reservation.template.id)

    escalation_provisions_qs = []

    for index, el in enumerate(params.provideStrategy):
        qs = escalation_provisions_qs[index - 1] if index != 0 else template_qs

        if el == ProvideTactic.FILTER_OWN:
            escalation_provisions_qs.append(
                qs.filter(registry__user=reservation.creator)
            )

        if el == ProvideTactic.FILTER_TEMPLATES:
            if not params.templates or len(params.templates) == 0:
                escalation_provisions_qs.append(qs.all())
            else:
                escalation_provisions_qs.append(qs.filter(pk__in=params.templates))

        if el == ProvideTactic.FILTER_AGENTS:
            if not params.agents or len(params.agents) == 0:
                escalation_provisions_qs.append(qs.all())
            else:
                escalation_provisions_qs.append(
                    qs.filter(registry__agents__pk__in=params.agents)
                )

        if el == ProvideTactic.ALL:
            escalation_provisions_qs.append(qs.all())

        if el == ProvideTactic.FILTER_ACTIVE_AGENTS:
            escalation_provisions_qs.append(
                qs.filter(registry__agents__status__in=[AgentStatus.ACTIVE])
            )
        if el == ProvideTactic.BALANCE:
            qs.annotate(len_provisions=Count("provisions")).order_by("len_provisions")

    return escalation_provisions_qs


def prepare_events_for_provision_map(
    reservation: Reservation, mapped_prov: Provision
) -> List[MessageEvent]:
    events = []
    if mapped_prov.status == ProvisionStatus.ACTIVE:
        # If the Agent is active we can just forward it to him else we are waiting for the
        # connect by just saving it in the database
        bounced_reserve = BouncedReserveMessage(
            data={
                "node": reservation.node.id if reservation.node else None,
                "template": reservation.template.id if reservation.template else None,
                "provision": mapped_prov.id,
            },
            meta={
                "reference": reservation.reference,
                "extensions": reservation.extensions,
                "context": reservation.context,
            },
        )

        forwarded_reserve = ReserveLogMessage(
            data={
                "level": LogLevel.INFO,
                "message": f"Provision {mapped_prov.title} ({mapped_prov.id}) is online. Fowarding Request",
            },
            meta={
                "reference": reservation.reference,
            },
        )

        events.append(
            MessageEvent(
                reservation.callback,
                forwarded_reserve,
            )
        )

        events.append(
            MessageEvent(
                f"reservations_in_{mapped_prov.unique}",
                bounced_reserve,
            )
        )

    else:
        # This happens when the provision is not active at this moment,
        # The agent will only reserve the message once connected
        # In theory this can also be handled by persistence in the queue
        # but that would make one part stateful, lets use the database for now
        mapped_prov.reservations.add(reservation)

        waiting_log_message = ReserveLogMessage(
            data={
                "level": LogLevel.INFO,
                "message": f"Waiting for Provision {mapped_prov.title} ({mapped_prov.id}) to come online",
            },
            meta={
                "reference": reservation.reference,
            },
        )

        events.append(
            MessageEvent(
                reservation.callback,
                waiting_log_message,
            )
        )

    return events


def predicate_fullfiled(
    reservation: Reservation,
    params: ReserveParams,
    mapped_provision: Dict[str, Provision],
):

    return len(mapped_provision.items()) >= params.desiredInstances


def predicate_unprovide(
    reservation: Reservation,
    prov: Provision,
):

    return prov.reservations.count() == 0


def escalate_through_reserve_qs(
    querysets: List[QuerySet],
    reservation: Reservation,
    params: ReserveParams,
    mapped_provisions: Dict[str, Provision] = {},
) -> Tuple[Dict[str, Provision], List[MessageEvent]]:

    events = []
    fullfilled = False  # check if we fullfiled our conditions of the ReserveParams
    for qs in reversed(querysets):
        if fullfilled:
            break
        logger.info(f"Trying with {qs} ")
        for prov in qs:
            if prov.id not in mapped_provisions:

                logger.info(
                    f"Mapping already existing Provision {prov}  to {reservation}"
                )
                mapped_provisions[prov.id] = prov
                events += prepare_events_for_provision_map(reservation, prov)

                if predicate_fullfiled(reservation, params, mapped_provisions):
                    fullfilled = True
                    break

    return mapped_provisions, events


def prepare_events_for_new_provision(
    reservation: Reservation, prov: Provision
) -> List[MessageEvent]:
    events = []

    if prov.bound.status == AgentStatus.ACTIVE:
        # If the Agent is active we can just forward it to him else we are waiting for the
        # connect by just saving it in the database
        provide_message = BouncedProvideMessage(
            data={
                "template": prov.template.id,
                "params": prov.params,
            },
            meta={
                "reference": prov.reference,
                "extensions": prov.extensions,
                "context": prov.context,
            },
        )

        forwarding_message = ReserveLogMessage(
            data={
                "level": LogLevel.INFO,
                "message": f"Reservation caused new Provision {prov.title} ({prov.id}) whose Agent is online. Fowarding it",
            },
            meta={
                "reference": reservation.reference,
            },
        )

        events.append(
            MessageEvent(
                reservation.callback,
                forwarding_message,
            )
        )

        events.append(
            MessageEvent(
                f"provision_agent_in_{prov.bound.unique}",
                provide_message,
            )
        )

    else:

        waiting_log_message = ReserveLogMessage(
            data={
                "level": LogLevel.INFO,
                "message": f"Reservation caused new Provision {prov.title} ({prov.id}) whose Agent is OFFLINE. Waiting for it to connect",
            },
            meta={
                "reference": reservation.reference,
            },
        )

        events.append(
            MessageEvent(
                reservation.callback,
                waiting_log_message,
            )
        )

    return events


def escalate_through_provision_qs(
    querysets: List[QuerySet],
    reservation: Reservation,
    params: ReserveParams,
    mapped_provisions: Dict[str, Provision] = {},
) -> Tuple[Dict[str, Provision], List[MessageEvent]]:

    already_mapped = len(mapped_provisions.items())

    events = []
    fullfilled = False  # check if we fullfiled our conditions of the ReserveParams
    for qs in reversed(querysets):
        if fullfilled:
            break
        logger.info(f"Trying with {qs} ")
        for template in qs:
            template_params = TemplateParams(**template.params)

            maximum_instances = template_params.maximumInstances or 1
            maximum_agent_instances = template_params.maximumInstancesPerAgent or 1

            total_prov_count = template.provisions.count()

            for agent in template.registry.agents.all():
                if fullfilled:
                    break

                agent_prov_count = (
                    agent.bound_provisions.filter(template=template)
                    .exclude(status=ProvisionStatus.CANCELLED)
                    .count()
                )  # only the provisions of that template count, also of course jsut the active ones

                while agent_prov_count < maximum_agent_instances:
                    prov = Provision.objects.create(
                        title=reservation.title,
                        reservation=reservation,
                        status=ProvisionStatus.PENDING,
                        reference=str(
                            uuid4()
                        ),  # We use the same reference that the user wants
                        context=reservation.context,  # We use the same reference that the user wants
                        template=template,
                        bound=agent,
                        params=reservation.params,
                        extensions=reservation.extensions,
                        app=reservation.app,
                        creator=reservation.creator,
                        callback=None,
                    )
                    if prov.creator:
                        MyProvisionsEvent.broadcast(
                            {"action": "created", "data": prov.id},
                            [f"provisions_user_{prov.creator.id}"],
                        )

                    prov.reservations.add(reservation)  # We always add the provision
                    logger.info(
                        f"Mapping newly created Provision {prov} to {reservation}"
                    )
                    mapped_provisions[prov.id] = prov
                    agent_prov_count += 1

                    events += prepare_events_for_new_provision(reservation, prov)

                    if predicate_fullfiled(reservation, params, mapped_provisions):
                        fullfilled = True
                        break

    return mapped_provisions, events
