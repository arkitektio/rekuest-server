from facade.filters import PodFilter, TemplateFilter
from typing_extensions import Annotated
from balder.types import BalderQuery
from facade import types
from facade.enums import PodStatus
from facade.models import Template
import graphene
from herre import bounced
from balder.enum import InputEnum

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