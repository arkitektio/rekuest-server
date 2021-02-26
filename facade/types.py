from facade.structures.ports.ins.types import InPort
from facade.structures.ports.out.types import OutPort
from facade import models
from balder.types import BalderObject
import graphene

class DataPoint(BalderObject):

    class Meta:
        model = models.DataPoint


class DataModel(BalderObject):

    class Meta:
        model = models.DataModel


class DataQuery(graphene.ObjectType):
    point = graphene.Field(DataPoint, description="The queried Datapoint")
    models = graphene.List(DataModel, description="The queried models on the Datapoint")



class Node(BalderObject):
    inputs = graphene.List(InPort)
    outputs = graphene.List(OutPort)

    class Meta:
        model = models.Node


class Template(BalderObject):
    
    class Meta:
        model = models.Template

class Provider(BalderObject):
    
    class Meta:
        model = models.Provider


class Pod(BalderObject):
    
    class Meta:
        model = models.Pod