from facade.filters import TemplateFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Template, Node
import graphene
from lok import bounced
from guardian.shortcuts import get_objects_for_user
from facade.inputs import ProvisionStatusInput

class TemplateDetailQuery(BalderQuery):
    class Arguments:
        id = graphene.ID(description="The query pod")

    @bounced(anonymous=True)
    def resolve(root, info, id=None):
        return Template.objects.get(id=id)

    class Meta:
        type = types.Template
        operation = "template"


class Templates(BalderQuery):
    class Meta:
        type = types.Template
        list = True
        paginate = True
        filter = TemplateFilter


class ReservableTemplates(BalderQuery):

    class Arguments:
        node = graphene.ID(description="The node provisions", required=False)
        hash = graphene.String(description="The hash of the template", required=False)

    @bounced()
    def resolve(root, info, node=None, hash=None):
        assert node or hash, "You must provide a node or a hash"
        template_queryset = get_objects_for_user(
            info.context.user,
            "facade.providable",
        )
        if hash:
            return template_queryset.filter(node__hash=hash)

        return template_queryset.filter(node__id=node)



    class Meta:
        type = types.Template
        list = True
        filter = TemplateFilter