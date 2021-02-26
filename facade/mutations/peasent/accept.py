from facade.enums import PodStatus
from facade.models import Provider, Template, Pod
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced

class Accept(BalderMutation):
    """ Accepting is a way for a provider to create a pod, this can be instatiated through a provision or simply
    through providing a template"""

    class Arguments:
        template = graphene.ID(required=True, description="The Template you are giving an implementation for!")

    @bounced(only_jwt=True)
    def mutate(root, info, template=None, provider=None):
        provider = Provider.objects.get(app=info.context.auth.client_id, user=info.context.user)
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


    class Meta:
        type = types.Pod