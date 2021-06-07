from django.contrib.auth import get_user_model
from facade.filters import TemplateFilter, ProvisionFilter
from balder.fields.filtered import BalderFiltered
from django.utils.translation import templatize
from facade.structures.ports.returns.types import ReturnPort
from facade.structures.ports.kwargs.types import KwargPort
from facade.structures.ports.args.types import ArgPort
from facade import models
from herre.models import HerreApp as HerreAppModel
from balder.types import BalderObject
import graphene


class ReserveParamsInput(graphene.InputObjectType):
    auto_provide = graphene.Boolean(description="Do you want to autoprovide", required=False)
    auto_unprovide = graphene.Boolean(description="Do you want to auto_unprovide", required=False)
    providers = graphene.List(graphene.Int, description="Apps that you can reserve on", required=False)
    templates = graphene.List(graphene.Int, description="Apps that you can reserve on", required=False)

class ReserveParams(graphene.ObjectType):
    auto_provide = graphene.Boolean(description="Autoproviding")
    auto_unprovide = graphene.Boolean(description="Autounproviding")

class ProvideParams(graphene.ObjectType):
    auto_unprovide = graphene.Boolean(description="Do you want to auto_unprovide")



class HerreApp(BalderObject):

    class Meta:
        model = HerreAppModel


class HerreUser(BalderObject):

    class Meta:
        model = get_user_model()


class DataPoint(BalderObject):
    distinct = graphene.String()

    def resolve_distinct(root, info, *args, **kwargs):
        return root.app.name

    class Meta:
        model = models.DataPoint


class DataModel(BalderObject):


    class Meta:
        model = models.DataModel

class Scan(graphene.ObjectType):
    ok = graphene.Boolean()


class DataQuery(graphene.ObjectType):
    point = graphene.Field(DataPoint, description="The queried Datapoint")
    models = graphene.List(DataModel, description="The queried models on the Datapoint")


class Provider(BalderObject):
    
    class Meta:
        model = models.Provider

class Repository(BalderObject):

    class Meta:
        model = models.Repository

class Provision(BalderObject):
    params = graphene.Field(ProvideParams)
    
    class Meta:
        model = models.Provision

        
class Template(BalderObject):
    provisions = BalderFiltered(Provision, filterset_class=ProvisionFilter, related_field="provisions")
    
    class Meta:
        model = models.Template

class Node(BalderObject):
    args = graphene.List(ArgPort)
    kwargs = graphene.List(KwargPort)
    returns = graphene.List(ReturnPort)
    templates = BalderFiltered(Template, filterset_class=TemplateFilter, related_field="templates")

    class Meta:
        model = models.Node


class Accessor(BalderObject):

    class Meta:
        model = models.Accessor


class Reservation(BalderObject):
    params = graphene.Field(ReserveParams)
    
    class Meta:
        model = models.Reservation


class ReservationLog(BalderObject):

    class Meta:
        model = models.ReservationLog
        

class Assignation(BalderObject):
    
    class Meta:
        model = models.Assignation


class AssignationLog(BalderObject):
    
    class Meta:
        model = models.AssignationLog

class ProvisionLog(BalderObject):
    
    class Meta:
        model = models.ProvisionLog


