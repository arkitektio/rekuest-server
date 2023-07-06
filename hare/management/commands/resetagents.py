from django.core.management.base import BaseCommand
from django.conf import settings
from facade.models import Agent, AgentStatus
from hare.carrots import (
    HareMessage,
    ProvideHareMessage,
    ReserveHareMessage,
    KickHareMessage,
    UnprovideHareMessage,
    UnreserveHareMessage,
    ReservationChangedMessage,
)
from hare.connection import pikaconnection
from facade.utils import cascade_agent_failure

class Command(BaseCommand):
    help = "Resets all agents"

    def handle(self, *args, **kwargs):
        instance = settings.INSTANCE_NAME

        agents = Agent.objects.filter(on_instance=instance).all()
        for agent in agents:
            cascade_agent_failure(agent, AgentStatus.DISCONNECTED)
            agent.save()    
