from facade import types
from facade.models import Pod,  ServiceProvider, Template
from balder.types import BalderMutation
from facade.enums import PodStatus
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

class CreatePod(BalderMutation):
    """Create A Pod according to the specifications"""

    class Arguments:
        template = graphene.ID(description="The Template you want to create a pod for")


    class Meta:
        type = types.Pod

    
    @bounced(anonymous=True) #TODO: Whitelist this only for the service providers
    def mutate(root, info, template=None):
        provider = ServiceProvider.objects.get(name="port") #TODO: Get this from something
        template = Template.objects.get(id=template)

        pod, created = Pod.objects.update_or_create(
            name = f"{provider.name}-{template.id}",
            defaults= {
                "template": template,
                "status": PodStatus.PENDING,
            }
        )
        pod.save()

        return pod