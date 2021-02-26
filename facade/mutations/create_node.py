from facade.structures.ports.input import InPortInput, OutPortInput
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
    repo, _ = Repository.objects.filter(creator=user).get_or_create(type=f"flow", defaults={"name": f"flow_{id}", "creator": user})
    return repo

class CreateNode(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        description = graphene.String(description="A description for the Node", required=False)
        name = graphene.String(description="The name of this template", required=True)
        outputs = graphene.List(OutPortInput, description="The Outputs")
        inputs = graphene.List(InPortInput, description="The Inputs")
        type = graphene.Argument(InputEnum.from_choices(NodeType),description="The variety")
        interface = graphene.String(description="The Interface", required=True)
        package = graphene.String(description="The Package", required=True)


    class Meta:
        type = types.Node

    
    @bounced()
    def mutate(root, info, package=None, interface=None, description="Not description", outputs=[], inputs=[], type=None, name="name"):
        repository = get_node_repository(info.context.user)
        
        node, created = Node.objects.update_or_create(package=package, interface=interface, repository=repository, defaults={
            "description": description,
            "outputs": outputs,
            "inputs": inputs,
            "name": name
        })
   
        return node