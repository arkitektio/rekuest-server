from facade.structures.ports.input import ArgPortInput, KwargPortInput, ReturnPortInput
from facade import types
from facade.models import AppRepository, Structure, Repository, Node
from balder.types import BalderMutation
from balder.enum import InputEnum
from facade.enums import NodeType, NodeTypeInput
from lok import bounced
import graphene
import logging
import inflection

logger = logging.getLogger(__name__)


class CreateNode(BalderMutation):
    """Create Node according to the specifications"""

    class Arguments:
        description = graphene.String(
            description="A description for the Node", required=False
        )
        name = graphene.String(description="The name of this template", required=True)
        args = graphene.List(ArgPortInput, description="The Args")
        kwargs = graphene.List(KwargPortInput, description="The Kwargs")
        returns = graphene.List(ReturnPortInput, description="The Returns")
        interfaces = graphene.List(
            graphene.String,
            description="The Interfaces this node provides [eg. bridge, filter]",
        )  # todo infer interfaces from args kwargs
        type = graphene.Argument(
            NodeTypeInput,
            description="The variety",
            default_value=NodeType.FUNCTION.value,
        )
        interface = graphene.String(description="The Interface", required=True)
        package = graphene.String(description="The Package", required=False)

    class Meta:
        type = types.Node

    @bounced(anonymous=True)
    def mutate(
        root,
        info,
        package=None,
        interface=None,
        description="Not description",
        args=[],
        kwargs=[],
        returns=[],
        interfaces=[],
        type=None,
        name="name",
    ):
        repository, _ = AppRepository.objects.update_or_create(
            app=info.context.bounced.app,
            defaults={"name": inflection.underscore(info.context.bounced.app.name)},
        )

        arg_identifiers = [arg.identifier for arg in args if arg.identifier]
        kwarg_identifiers = [kwarg.identifier for kwarg in kwargs if kwarg.identifier]
        return_identifiers = [
            returnitem.identifier for returnitem in returns if returnitem.identifier
        ]

        all_identifiers = set(arg_identifiers + kwarg_identifiers + return_identifiers)
        for identifier in all_identifiers:
            try:
                model = Structure.objects.get(identifier=identifier)
            except Structure.DoesNotExist:
                # assert "can_create_identifier" in info.context.bounced.scopes, "You cannot create a new DataModel if you dont have the 'can_create_identifier' scopes"
                new_structure = Structure.objects.create(
                    repository=repository, identifier=identifier
                )
                logger.info(f"Created {new_structure}")

        node, created = Node.objects.update_or_create(
            package=repository.name,
            interface=interface,
            repository=repository,
            defaults={
                "description": description,
                "args": args,
                "kwargs": kwargs,
                "returns": returns,
                "name": name,
                "type": type,
            },
            interfaces=interfaces,
        )

        return node


class DeleteNodeReturn(graphene.ObjectType):
    id = graphene.String()


class DeleteNode(BalderMutation):
    """Create an experiment (only signed in users)"""

    class Arguments:
        id = graphene.ID(
            description="A cleartext description what this representation represents as data",
            required=True,
        )

    @bounced()
    def mutate(root, info, id, **kwargs):
        node = Node.objects.get(id=id)
        node.delete()
        return {"id": id}

    class Meta:
        type = DeleteNodeReturn
