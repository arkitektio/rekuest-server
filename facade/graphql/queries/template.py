from facade.filters import TemplateFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Template, Node, Registry, Agent
import graphene
from lok import bounced
from guardian.shortcuts import get_objects_for_user
from facade.inputs import ProvisionStatusInput, TemplateParamInput


class TemplateDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Template.objects.get(id=id)

    class Meta:
        type = types.Template
        operation = "template"



class MyTemplateForQuery(BalderQuery):
    """Asss

    Is A query for all of these specials in the world
    """

    class Arguments:
        id = graphene.ID(description="The query node")
        hash = graphene.String(description="The query node")
        instance_id = graphene.ID(description="The instance id", required=True)

    def resolve(root, info, instance_id=None, **kwargs):
        user = info.context.user

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
        node = Node.objects.get(**kwargs)
        return Template.objects.get(node=node, agent=agent)

    class Meta:
        type = types.Template
        operation = "mytemplatefor"


class Templates(BalderQuery):
    class Meta:
        type = types.Template
        list = True
        paginate = True
        filter = TemplateFilter


class ReservableTemplates(BalderQuery):
    class Arguments:
        template = graphene.ID(
            description="The template to reserve (This will assert if the template is truly reservable)",
            required=False,
        )
        node = graphene.ID(description="The node provisions", required=False)
        hash = graphene.String(description="The hash of the template", required=False)
        template_params = graphene.List(TemplateParamInput, required=False)

    @bounced()
    def resolve(root, info, node=None, hash=None, template_params=None, template=None):
        assert (
            node or hash or template_params or template
        ), "You must provide a node or a hash or template_params"
        qs = get_objects_for_user(
            info.context.user,
            "facade.providable",
        )

        if node:
            qs = qs.filter(node__id=node)

        if template:
            qs = qs.filter(id=template)

        if hash:
            qs = qs.filter(node__hash=hash)

        if template_params:
            for param in template_params:
                if param.value:
                    qs = qs.filter(
                        **{f"params__{param.key}": param.value}
                    )  # TODO: Do not allow nested keys?
                else:
                    qs = qs.filter(**{f"params__has_key": param.key})

        return qs

    class Meta:
        type = types.Template
        list = True
        filter = TemplateFilter
