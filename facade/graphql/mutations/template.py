from facade.models import Registry, Template, Structure, Node, Agent, Collection
from facade import types
from balder.types import BalderMutation
import graphene
from lok import bounced
from graphene.types.generic import GenericScalar
from facade.inputs import DefinitionInput
import logging
import json
import hashlib

logger = logging.getLogger(__name__)
from facade.utils import get_imitiate


def traverse_scope(port: dict, scope="LOCAL"):
    if port["kind"] == "STRUCTURE":
        if port["scope"] == scope:
            return True
    if "child" in port and port["child"]:
        return traverse_scope(port["child"], scope)
    return False


def has_locals(ports: list):
    for port in ports:
        print(port)
        if traverse_scope(port, "LOCAL"):
            return True
    return False


class CreateTemplate(BalderMutation):
    class Arguments:
        definition = graphene.Argument(DefinitionInput, required=True)
        interface = graphene.String(required=True)
        instance_id = graphene.ID(description="The instance id", required=True)
        extensions = graphene.List(
            graphene.String,
            description="Desired Extensions (e.g reaktion)",
            required=False,
        )
        params = GenericScalar(
            required=False, description="Some additional Params for your offering"
        )
        policy = GenericScalar(
            required=False, description="Some additional Params for your offering"
        )
        imitate = graphene.ID(description="User to imitate", required=False)

    @bounced(only_jwt=True)
    def mutate(
        root,
        info,
        definition: DefinitionInput,
        interface,
        params=None,
        policy=None,
        extensions=None,
        imitate=None,
        instance_id="main",
    ):
        args = definition.args or []
        returns = definition.returns or []
        description = definition.description or "No Description"
        name = definition.name
        interfaces = definition.interfaces or []
        kind = definition.kind
        pure = definition.pure == True
        port_groups = definition.port_groups or []
        extensions = extensions or []
        print("The port groups", port_groups)

        user = (
            info.context.user
            if imitate is None
            else get_imitiate(info.context.user, imitate)
        )

        registry, _ = Registry.objects.update_or_create(
            client=info.context.bounced.client,
            user=user,
            defaults=dict(app=info.context.bounced.app),
        )

        agent, _ = Agent.objects.update_or_create(
            registry=registry,
            instance_id=instance_id,
            defaults=dict(
                name=f"{str(registry)} on {instance_id}",
            ),
        )

        hashable_definition = {
            key: value
            for key, value in dict(definition).items()
            if key not in ["meta", "interface"]
        }

        hash = hashlib.sha256(
            json.dumps(hashable_definition, sort_keys=True).encode()
        ).hexdigest()
        print(hash)

        template = Template.objects.filter(interface=interface, agent=agent).first()

        if template:
            if template.node.hash != hash:
                if template.node.templates.count() == 1:
                    logger.info("Deleting Node because it has no more templates")
                    template.node.delete()

        try:
            node = Node.objects.get(hash=hash)
        except Node.DoesNotExist:
            has_local_argports = has_locals(args)
            has_local_returnports = has_locals(returns)

            if has_local_argports and has_local_returnports:
                scope = "LOCAL"
            if not has_local_argports and not has_local_returnports:
                scope = "GLOBAL"
            if not has_local_argports and has_local_returnports:
                scope = "BRIDGE_GLOBAL_TO_LOCAL"
            if has_local_argports and not has_local_returnports:
                scope = "BRIDGE_LOCAL_TO_GLOBAL"

            # arg_identifiers = [arg.identifier for arg in args if arg.identifier]
            # return_identifiers = [
            #     returnitem.identifier for returnitem in returns if returnitem.identifier
            # ]

            # all_identifiers = set(arg_identifiers + return_identifiers)
            # for identifier in all_identifiers:
            #     try:
            #         model = Structure.objects.get(identifier=identifier)
            #     except Structure.DoesNotExist:
            #         # assert "can_create_identifier" in info.context.bounced.scopes, "You cannot create a new DataModel if you dont have the 'can_create_identifier' scopes"
            #         new_structure = Structure.objects.create(
            #             identifier=identifier
            #         )
            #         logger.info(f"Created {new_structure}")

            node = Node.objects.create(
                hash=hash,
                description=description,
                args=args,
                scope=scope,
                kind=kind,
                pure=pure,
                port_groups=port_groups,
                interfaces=interfaces,
                returns=returns,
                name=name,
            )

            if definition.is_test_for:
                for nodehash in definition.is_test_for:
                    node.is_test_for.add(Node.objects.get(hash=nodehash))

            if definition.collections:
                for collection_name in definition.collections:
                    c, _ = Collection.objects.get_or_create(name=collection_name)
                    node.collections.add(c)

            logger.info(f"Created {node}")

        try:
            template = Template.objects.get(interface=interface, agent=agent)
            template.node = node
            template.extensions = extensions
            template.params = params or {}
            template.save()

        except Template.DoesNotExist:
            template = Template.objects.create(
                interface=interface,
                node=node,
                params=params or {},
                agent=agent,
                extensions=extensions,
            )

        return template

    class Meta:
        type = types.Template
