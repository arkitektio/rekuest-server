from facade.structures.ports.returns.types import ReturnPort
from facade.structures.ports.kwargs.types import KwargPort
from facade.structures.ports.args.types import ArgPort
from facade import models
from balder.types import BalderObject
import graphene

class DataPoint(BalderObject):

    class Meta:
        model = models.DataPoint


class DataModel(BalderObject):

    class Meta:
        model = models.DataModel


class Service(BalderObject):

    class Meta:
        model = models.Service


class Scan(graphene.ObjectType):
    ok = graphene.Boolean()


class DataQuery(graphene.ObjectType):
    point = graphene.Field(DataPoint, description="The queried Datapoint")
    models = graphene.List(DataModel, description="The queried models on the Datapoint")



class Node(BalderObject):
    args = graphene.List(ArgPort)
    kwargs = graphene.List(KwargPort)
    returns = graphene.List(ReturnPort)

    class Meta:
        model = models.Node

class Template(BalderObject):
    
    class Meta:
        model = models.Template

class Reservation(BalderObject):
    
    class Meta:
        model = models.Reservation


class Provider(BalderObject):
    
    class Meta:
        model = models.Provider


class AppProvider(BalderObject):
    
    class Meta:
        model = models.AppProvider

class Pod(BalderObject):
    
    class Meta:
        model = models.Pod