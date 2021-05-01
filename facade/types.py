from django.contrib.auth import get_user_model
from facade.filters import TemplateFilter
from balder.fields.filtered import BalderFiltered
from django.utils.translation import templatize
from facade.structures.ports.returns.types import ReturnPort
from facade.structures.ports.kwargs.types import KwargPort
from facade.structures.ports.args.types import ArgPort
from facade import models
from herre.models import HerreApp as HerreAppModel
from balder.types import BalderObject
import graphene


class HerreApp(BalderObject):

    class Meta:
        model = HerreAppModel


class HerreUser(BalderObject):

    class Meta:
        model = get_user_model()


class DataPoint(BalderObject):

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


class Repository(BalderObject):

    class Meta:
        model = models.Repository

        
class Template(BalderObject):
    
    class Meta:
        model = models.Template

class Node(BalderObject):
    args = graphene.List(ArgPort)
    kwargs = graphene.List(KwargPort)
    returns = graphene.List(ReturnPort)
    templates = BalderFiltered(Template, filterset_class=TemplateFilter, related_field="templates")

    class Meta:
        model = models.Node



class Reservation(BalderObject):
    
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


class Provider(BalderObject):
    
    class Meta:
        model = models.Provider


class Pod(BalderObject):
    
    class Meta:
        model = models.Pod