from facade.inputs import ArgPortInput, DefinitionInput, KwargPortInput, ReturnPortInput
from facade import types
from facade.models import AppRepository, Node, Structure
from balder.types import BalderMutation
from lok import bounced
import graphene
import logging
import inflection

logger = logging.getLogger(__name__)


class DefineNode(BalderMutation):
    """Defines a node according to is definition"""

    class Arguments:
        definition = graphene.Argument(DefinitionInput, required=True)

    @bounced(anonymous=True)
    def mutate(root, info, definition: DefinitionInput):

        args = definition.args or []
        kwargs = definition.kwargs or []
        returns = definition.returns or []
        interface = definition.interface
        package = definition.package
        description = definition.description or "No Description"
        name = definition.name
        interfaces = definition.interfaces or []
        kind = definition.kind

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
            package=package or f"@{repository.name}",
            interface=interface,
            defaults={
                "description": description,
                "args": args,
                "kwargs": kwargs,
                "returns": returns,
                "name": name,
                "kind": kind,
                "repository": repository,
                "interfaces": interfaces,
            },
        )

        return node

    class Meta:
        type = types.Node
        operation = "define"


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
