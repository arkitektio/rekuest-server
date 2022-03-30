from facade.filters import TemplateFilter
from balder.types import BalderQuery
from facade import types
from facade.models import Template
import graphene
from lok import bounced


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
        filter = TemplateFilter
