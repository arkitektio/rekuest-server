from facade.enums import PodStatus
from facade.models import AppProvider, Template, Pod, Provision
from facade import types
from balder.types import BalderMutation
import graphene
from herre import bounced

class Accept(BalderMutation):
    """ Accepting is a way for a provider to create a pod, this can be instatiated through a provision or simply
    through providing a template"""

    class Arguments:
        template = graphene.ID(required=True, description="The Template you are giving an implementation for!")
        provision = graphene.String(required=True, description="The Provision we need")

    @bounced(only_jwt=True)
    def mutate(root, info, template=None, provision=None):
        
        provision = Provision.objects.get(reference=provision)

        pod = Pod.objects.create(**{
                "template_id": template,
                "status": PodStatus.PENDING,
                "provision": provision
            }
        )

        reservation = provision.reservation
        pod.reservations.add(reservation)

        pod.save()

        print(pod)

        return pod


    class Meta:
        type = types.Pod