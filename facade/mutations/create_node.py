from facade.structures.ports.input import ArgPortInput, KwargPortInput, ReturnPortInput
from facade import types
from facade.models import Repository, Node
from balder.types import BalderMutation
from balder.enum import InputEnum
from facade.enums import NodeType
from herre import bounced
import graphene
import logging

logger = logging.getLogger(__name__)

def get_node_repository(user, id="localhost"):
    if user.is_anonymous:
        repo, _ = Repository.objects.get_or_create(type=f"flow", defaults={"name": f"flow_{id}"})
        return repo



    repo, _ = Repository.objects.filter(creator=user).get_or_create(type=f"flow", defaults={"name": f"flow_{id}", "creator": user})
    return repo

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
        package = graphene.String(description="The Package", required=True)



    class Meta:
        type = types.Node

    
    @bounced(anonymous=True)
    def mutate(root, info, package=None, interface=None, description="Not description", args=[], kwargs=[], returns=[], type=None, name="name"):
        repository = get_node_repository(info.context.user)
        
        print(type)
        node, created = Node.objects.update_or_create(package=package, interface=interface, repository=repository, defaults={
            "description": description,
            "args": args,
            "kwargs": kwargs,
            "returns": returns,
            "name": name,
            "type": type
        })
   
        return node