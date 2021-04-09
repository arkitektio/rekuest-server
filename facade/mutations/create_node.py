from facade.structures.ports.input import ArgPortInput, KwargPortInput, ReturnPortInput
from facade import types
from facade.models import AppRepository, Node
from balder.types import BalderMutation
from balder.enum import InputEnum
from facade.enums import NodeType
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

class CreateNode(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        description = graphene.String(description="A description for the Node", required=False)
        name = graphene.String(description="The name of this template", required=True)
        args = graphene.List(ArgPortInput, description="The Args")
        kwargs = graphene.List(KwargPortInput, description="The Kwargs")
        returns = graphene.List(ReturnPortInput, description="The Returns")
        type = graphene.Argument(InputEnum.from_choices(NodeType),description="The variety", default_value=NodeType.FUNCTION.value)
        interface = graphene.String(description="The Interface", required=True)
        package = graphene.String(description="The Package", required=False)



    class Meta:
        type = types.Node

    
    @bounced(anonymous=True)
    def mutate(root, info, package=None, interface=None, description="Not description", args=[], kwargs=[], returns=[], type=None, name="name"):
        if info.context.bounced.user is not None:
            app_name = info.context.bounced.app_name + " by " + info.context.bounced.user.username
        else:
            app_name = info.context.bounced.app_name
        
        
        repository , _ = AppRepository.objects.update_or_create(client_id=info.context.bounced.client_id, user=info.context.user, defaults= {"name": app_name})
        
        node, created = Node.objects.update_or_create(package=repository.name, interface=interface, repository=repository, defaults={
            "description": description,
            "args": args,
            "kwargs": kwargs,
            "returns": returns,
            "name": name,
            "type": type
        })

        print(node)
   
        return node