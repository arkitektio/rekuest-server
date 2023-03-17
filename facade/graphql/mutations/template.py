from facade.models import Registry, Template, Structure, Node, Agent
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


class CreateTemplate(BalderMutation):
    class Arguments:
        definition = graphene.Argument(DefinitionInput, required=True)
        instance_id = graphene.ID(description="The instance id", required=True)
        extensions = graphene.List(
            graphene.String, description="Desired Extensions", required=False
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
        definition,
        params=None,
        policy=None,
        extensions=[],
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


        user = info.context.user if imitate is None else get_imitiate(info.context.user, imitate)

        registry, _ = Registry.objects.update_or_create(
            client=info.context.bounced.client, user=user, defaults=dict(app=info.context.bounced.app)
        )

        agent, _ = Agent.objects.update_or_create(
            registry = registry, instance_id = instance_id, name="default"
        )


        hashable_definition = {key: value for key, value in dict(definition).items() if key not in ["meta","interface"]}


        hash = hashlib.sha256(json.dumps(hashable_definition, sort_keys=True).encode()).hexdigest()
        print(hash)

        template = Template.objects.filter(interface=definition.interface, agent=agent).first()

        if template:
            if template.node.hash == hash:
                return template
            else:
                if template.node.templates.count() == 1:
                    logger.info("Deleting Node because it has no more templates")
                    template.node.delete()

        try:
            node = Node.objects.get(hash=hash)
        except Node.DoesNotExist:
            arg_identifiers = [arg.identifier for arg in args if arg.identifier]
            return_identifiers = [
                returnitem.identifier for returnitem in returns if returnitem.identifier
            ]

            all_identifiers = set(arg_identifiers + return_identifiers)
            for identifier in all_identifiers:
                try:
                    model = Structure.objects.get(identifier=identifier)
                except Structure.DoesNotExist:
                    # assert "can_create_identifier" in info.context.bounced.scopes, "You cannot create a new DataModel if you dont have the 'can_create_identifier' scopes"
                    new_structure = Structure.objects.create(
                        identifier=identifier
                    )
                    logger.info(f"Created {new_structure}")


            node = Node.objects.create(
                hash=hash,
                description=description,
                args=args,
                kind=kind,
                pure=pure,
                interfaces=interfaces,
                returns=returns,
                name=name,
            )
            logger.info(f"Created {node}")




        
        try:
            template = Template.objects.get(
                interface=definition.interface, agent=agent
            )
            template.node = node
            template.extensions = extensions
            template.params = params or {}
            template.save()

        except:
            template = Template.objects.create(
                interface=definition.interface,
                node=node,
                params=params or {},
                agent=agent,
                extensions=extensions,
            )

        return template

    class Meta:
        type = types.Template
