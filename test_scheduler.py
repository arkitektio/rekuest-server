import os
import sys
from uuid import uuid4
import django
from delt.messages.generics import Context
from delt.messages import ReserveLogMessage
from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from delt.messages.postman.reserve.reserve_transition import (
    ReserveState,
    ReserveTransitionMessage,
)

from delt.types import ProvideTactic, ReserveParams, ReserveTactic, TemplateParams
from facade.enums import AgentStatus, LogLevel, ProvisionStatus
import logging


logger = logging.getLogger(__name__)


def main():
    from facade.models import Reservation, Template, Provision
    from django.db.models import Count

    reservation = Reservation.objects.get(
        reference="d7b865fa-2db2-4a15-ace6-69a6d7d343ec"
    )
    params = ReserveParams(**reservation.params)
    context = Context(**reservation.context)
    node = reservation.node
    template = reservation.template

    messages = []
    print(params)
    print(context)

    provided_provisions = {}
    new_provisions = {}

    if node and not template:
        # The Node path is more generic, we are just filteing by the node and the params

        # Check for Active provisions
        logger.info(params.desiredInstances)
        logger.info(params.reserveStrategy)

        # Reservation Part
        provisions_qs = Provision.objects.filter(template__node=node)

        if len(params.templates) > 0:
            provisions_qs = provisions_qs.filter(template__pk__in=params.templates)

        if len(params.registries) > 0:
            provisions_qs = provisions_qs.filter(
                template__registry__pk__in=params.registries
            )

        escalation_reservation_qs = []

        for index, el in enumerate(params.reserveStrategy):
            qs = escalation_reservation_qs[index - 1] if index != 0 else provisions_qs

            if el == ReserveTactic.ALL:
                escalation_reservation_qs.append(qs.all())

            if el == ReserveTactic.FILTER_AGENTS:
                if len(params.agents) == 0:
                    escalation_reservation_qs.append(qs.all())
                else:
                    escalation_reservation_qs.append(
                        qs.filter(registry__agents__pk__in=params.agents)
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

        reached_amount = False
        for qs in reversed(escalation_reservation_qs):
            if reached_amount:
                break
            logger.info(f"Trying with {qs} ")
            for prov in qs:
                if prov.id not in provided_provisions:
                    logger.info("Adding new Reservation")

                    provided_provisions[prov.id] = prov

                    if prov.status == ProvisionStatus.ACTIVE:
                        # If the Agent is active we can just forward it to him else we are waiting for the
                        # connect by just saving it in the database
                        bounced_reserve = BouncedReserveMessage(
                            data={
                                "node": reservation.node.id,
                                "template": reservation.template.id,
                                "provision": prov.id,
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
                                "message": f"Provision {prov.title} ({prov.id}) is online. Fowarding Request",
                            },
                            meta={
                                "reference": reservation.reference,
                            },
                        )

                        messages.append(
                            (
                                reservation.callback,
                                forwarded_reserve,
                            )
                        )

                        messages.append(
                            (
                                f"reservations_in_{prov.unique}",
                                bounced_reserve,
                            )
                        )

                    else:
                        # This happens when the provision is not active at this moment,
                        # The agent will only reserve the message once connected
                        # In theory this can also be handled by persistence in the queue
                        # but that would make one part stateful, lets use the database for now
                        prov.reservations.add(reservation)

                        waiting_log_message = ReserveLogMessage(
                            data={
                                "level": LogLevel.INFO,
                                "message": f"Waiting for Provision {prov.title} ({prov.id}) to come online",
                            },
                            meta={
                                "reference": reservation.reference,
                            },
                        )

                        messages.append(
                            (
                                reservation.callback,
                                waiting_log_message,
                            )
                        )

                    if len(provided_provisions.items()) == params.desiredInstances:
                        # We have reached already the amount of desired instanced.
                        # We can breal
                        reached_amount = True
                        break

        logger.info("Found nothing new")

        if reached_amount:
            logger.info("We did it bitch! Didn't even have to reserve some shit")
            logger.info(new_provisions)
            logger.info(provided_provisions)
            return messages

        # Reserve Moment
        template_qs = Template.objects.filter(node=node)

        if len(params.templates) > 0:
            template_qs = template_qs.filter(pk__in=params.templates)

        if len(params.registries) > 0:
            template_qs = template_qs.filter(registry__pk__in=params.registries)

        escalation_provisions_qs = []

        logger.info(params.provideStrategy)
        for index, el in enumerate(params.provideStrategy):
            qs = escalation_provisions_qs[index - 1] if index != 0 else template_qs

            if el == ProvideTactic.FILTER_OWN:
                escalation_provisions_qs.append(
                    qs.filter(registry__user=reservation.creator)
                )

            if el == ProvideTactic.FILTER_AGENTS and len(params.agents) > 0:
                escalation_reservation_qs.append(
                    qs.filter(registry__agents__pk__in=params.agents)
                )

            if el == ProvideTactic.ALL:
                escalation_provisions_qs.append(qs.all())

            if el == ProvideTactic.FILTER_ACTIVE_AGENTS:
                escalation_provisions_qs.append(
                    qs.filter(registry__agents__status__in=[AgentStatus.ACTIVE])
                )
            if el == ProvideTactic.BALANCE:
                qs.annotate(len_provisions=Count("provisions")).order_by(
                    "len_provisions"
                )

        logger.info(escalation_provisions_qs)
        for qs in reversed(escalation_provisions_qs):
            if reached_amount:
                break
            logger.info(f"Trying with {qs} ")
            for template in qs:
                if reached_amount:
                    break

                logger.info(f"Testing ss Template {template}")

                template_params = TemplateParams(**template.params)

                maximum_instances = template_params.maximumInstances or 1
                maximum_agent_instances = template_params.maximumInstancesPerAgent or 1

                total_prov_count = template.provisions.count()

                for agent in template.registry.agents.all():
                    agent_prov_count = agent.bound_provisions.filter(
                        template=template
                    ).count()

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
                            callback=reservation.callback,
                        )
                        prov.reservations.add(reservation)

                        new_provisions[prov.id] = prov
                        logger.info(f"Created new Provisoin {prov}")
                        if (
                            len(provided_provisions.items())
                            + len(new_provisions.items())
                            == params.desiredInstances
                        ):
                            reached_amount = True
                            break
                        agent_prov_count += 1
                        total_prov_count += 1

        logger.info("We had to create some provisions but seems okay")
        logger.info(new_provisions)
        logger.info(provided_provisions)
        return messages


if __name__ == "__main__":
    sys.path.insert(0, "/workspace")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arkitekt.settings")
    django.setup()

    messages = main()
    for message in messages:
        logger.info(f"Created Message: {message}")
