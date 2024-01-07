from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from facade.models import Agent, ProvisionStatus, ReserveParams, ReservationStatus, AgentStatus
from hare.carrots import (
    HareMessage,
    ReservationChangedMessage
)
class ImitationError(PermissionError):
    pass


def get_imitiate(user: User, imitater: str):
    
    return get_user_model().objects.get(id=imitater)





def cascade_agent_failure(agent: Agent, agent_status: AgentStatus):
    """Cascades agent failure to all reservations and provisions"""

    forwards = []
    for provision in agent.provisions.exclude(
        status__in=[ProvisionStatus.CANCELLED, ProvisionStatus.ENDED]
    ).all():
        provision.status = ProvisionStatus.DISCONNECTED
        provision.save()

        for res in provision.reservations.all():
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
                forwards += [
                    ReservationChangedMessage(
                        queue=res.waiter.queue,
                        reservation=res.id,
                        status=res.status,
                    )
                ]

    agent.status = agent_status
    agent.save()

    return forwards