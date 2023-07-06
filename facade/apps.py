from django.apps import AppConfig
import logging

class FacadeConfig(AppConfig):
    name = "facade"


    def ready(self) -> None:
        from .models import Agent, AgentStatus
        from .utils import cascade_agent_failure
        from django.conf import settings


        instance = settings.INSTANCE_NAME

        try:
            agents = Agent.objects.filter(on_instance=instance).all()
            for agent in agents:
                cascade_agent_failure(agent, AgentStatus.DISCONNECTED)
                agent.save()    
        except Exception as e:
            logging.error(e)
            pass

            
        return super().ready()
