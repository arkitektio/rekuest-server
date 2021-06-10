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
        repository , _ = Repository.objects.update_or_create(app=info.context.bounced.app, user=info.context.bounced.user, defaults= {"name": info.context.bounced.app.name})
        
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


class DeleteNodeReturn(graphene.ObjectType):
    id = graphene.String()


class DeleteNode(BalderMutation):
    """ Create an experiment (only signed in users)
    """

    class Arguments:
        id = graphene.ID(description="A cleartext description what this representation represents as data", required=True)


    @bounced()
    def mutate(root, info, id, **kwargs):
        node =  Node.objects.get(id=id)
        node.delete()
        return {"id": id}


    class Meta:
        type = DeleteNodeReturn
