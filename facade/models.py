from herre.models import HerreApp
from django.db.models import constraints
from herre.token import JwtToken
from mars.names import generate_random_name
from facade.fields import ArgsField, InPortsField, InputsField, KwargsField, OutPortsField, OutputsField, ParamsField, PodChannel, ReturnField
from django.db.models.fields import NullBooleanField
from facade.enums import AssignationLogLevel, AssignationStatus, DataPointType, HookType, NodeType, PodMode, PodStatus, PodStrategy, ProvisionStatus, RepositoryType, ReservationLogLevel, ReservationStatus
from django.db import models
from django.contrib.auth import get_user_model
import uuid
import requests
import logging

logger = logging.getLogger(__name__)


class DataPoint(models.Model):
    """A Datapoint constitues Arkitekts Representation of a Host of Data.

    Datapoints host Datamodels, that in turn are accessible as Models for Node inputs and node outputs

    """
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, help_text="The Associated App")
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users", null=True)
    version = models.CharField(max_length=100, help_text="The version of the bergen API this endpoint uses")
    inward = models.CharField(max_length=100, help_text="Inward facing hostname (for Docker powered access)", null=True)
    outward = models.CharField(max_length=100, help_text="Outward facing hostname for external clients", null=True)
    port = models.IntegerField(help_text="Listening port", null=True)
    type = models.CharField(max_length=100, choices=DataPointType.choices, default=DataPointType.GRAPHQL, help_text="The type of datapoint")
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    needs_negotiation = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app","user"], name="No multiple AppPoints for same App and User allowed")
        ]

    def __str__(self):
        return f"{self.app} {f'for {self.user}' if self.user else ''}"


class DataModel(models.Model):
    """A Datamodel is a uniquely identifiable model for a Datapoint

    """
    point = models.ForeignKey(DataPoint, on_delete=models.CASCADE, related_name="models")
    extenders = models.JSONField(help_text="Registered Extenders on this Model")
    identifier = models.CharField(max_length=1000, help_text="A unique identifier for this model on the Datapoint")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["point","identifier"], name="unique identifier for point")
        ]

    def __str__(self):
        return f"{self.identifier} at {self.point}"


class Accessor(models.Model):
    model = models.OneToOneField(
        DataModel,
        on_delete=models.CASCADE,
    )
    get = models.TextField(max_length=2000, help_text="A get accessor for this model", null=True, blank=True)
    search = models.TextField(max_length=2000, help_text="A selectable options query with a search Parameter", null=True, blank=True)
    create = models.TextField(max_length=2000, help_text="A create Parameter for this model", null=True, blank=True)

    def __str__(self):
        return f"Acessor for {self.model}"


class Repository(models.Model):
    """ A Repository is the housing conatinaer for a Node, Multiple Nodes belong to one repository.

    Repositories can be replicas of online sources (think pypi repository), but also containers for
    local user generated nodes (think nodes that were generated through the flow provider)
    """
    name = models.CharField(max_length=1000, help_text="The name of this Repository")
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(max_length=1000, default=uuid.uuid4, help_text="A world-unique identifier")
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, help_text="The Associated App")
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users", null=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app","user"], name="No multiple Repositories for same App and User allowed")
        ]

    def __str__(self):
        return f"{self.name}"

class Provider(models.Model):
    """ A provider is the intermediate step from a template to a pod, it takes an associated template
    and transfers it to a pod, given the current restrictions of the setup"""
    name = models.CharField(max_length=2000, help_text="This providers Name", default="Nana")    
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(max_length=1000, default=uuid.uuid4, help_text="The Channel we are listening to")
    active = models.BooleanField(default=False, help_text="Is this Provider active right now?")
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, help_text="The Associated App")
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users", null=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app","user"], name="No multiple Providers for same App and User allowed")
        ]

    def __str__(self):
        return f"{self.name}"


class BaseHooker(models.Model):
    name = models.CharField(max_length=200, help_text="This hookers name", default="Nana")



class Node(models.Model):
    """ Nodes are abstraction of RPC Tasks. They provide a common API to deal with creating tasks.

    See online Documentation"""
    type = models.CharField(max_length=1000, choices=NodeType.choices, default=NodeType.FUNCTION, help_text="Function, generator? Check async Programming Textbook")
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, null=True, blank=True, related_name="nodes")
    channel = models.CharField(max_length=1000, default=uuid.uuid4, help_text="The unique channel where we can reach pods of this node [depending on Stragey]", unique=True)


    name = models.CharField(max_length=1000, help_text="The cleartext name of this Node")
    package = models.CharField(max_length=1000, help_text="Package (think Module)")
    interface = models.CharField(max_length=1000, help_text="Interface (think Function)")

    description = models.TextField(help_text="A description for the Node")
    image = models.ImageField(null=True, blank=True, help_text="A short description what this Node does")
    
    args = ArgsField(default=list, help_text="Inputs for this Node")
    kwargs = KwargsField(default=list, help_text="Inputs for this Node")
    returns = ReturnField(default=list, help_text="Outputs for this Node")
    
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository","package","interface"], name="package, interface, repository cannot be the same")
        ]

    def __str__(self):
        return f"Node: {self.name} - {self.package}/{self.interface}"



class Template(models.Model):
    """ A Template is a conceptual implementation of A Node. It represents its implementation as well as its performance"""

    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this template is implementatig", related_name="templates")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, help_text="The associated provider for this Template")
    name = models.CharField(max_length=1000, default=generate_random_name, help_text="A name for this Template")

    policy = models.JSONField(max_length=2000, default=dict, help_text="The attached policy for this template")

    params = ParamsField(default=dict, help_text="Params for this Template")
    channel = models.CharField(max_length=1000, default=uuid.uuid4, help_text="The unique channel where we can reach pods of this template [depending on Stragey]", unique=True)

    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, null=True, help_text="Who created this template on this instance")
    version = models.CharField(max_length=400, help_text="A short descriptor for the kind of version") #Subject to change
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["node","params","provider"], name="A template has unique params for every node")
        ]

    def __str__(self):
        return f"{self.node} implemented by {self.provider}"

    @property
    def is_active(self):
        return len(self.pods.all()) > 0

class Provision(models.Model):

    reservation = models.ForeignKey("Reservation", on_delete=models.CASCADE, null=True, blank=True, help_text="The Reservation that created this Provision", related_name="provisions")
    #1 Inputs to the the Provision (it can be either already a template to provision or just a node)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The node this provision connects", related_name="provisions", null=True, blank=True)
    
    # Selection criteria for finding a right Pod
    params = models.JSONField(null=True, blank=True, help_text="Params for the Policy (including Provider etc..)") 

    #Status Field
    status = models.CharField(max_length=300, choices=ProvisionStatus.choices, default=ProvisionStatus.PENDING.value, help_text="Current lifecycle of Provision")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Provision as for now", blank=True)

    #Callback
    callback =  models.CharField(max_length=1000, help_text="Callback", blank=True , null=True)
    progress =  models.CharField(max_length=1000, help_text="Provider", blank=True , null=True)
    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Provisions parent", related_name="children")
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="This provision creator", null=True, blank=True)
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Assignation")



    def __str__(self):
        return f"Provision for Template: {self.template if self.template else ''} Referenced {self.reference} || Reserved {self.reservation if self.reservation else 'without Reservation'}"





class Pod(models.Model):
    """ The last step in any provision, pods are running implementations of templates (think workers)"""
    provision = models.ForeignKey("Provision", on_delete=models.CASCADE, help_text="The provision that created this pod", related_name="created_pods", null=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The template that created this pod", related_name="pods")
    status = models.CharField(max_length=300, choices=PodStatus.choices, default=PodStatus.PENDING, help_text="Which lifecycle moment is this pod in")
    mode = models.CharField(max_length=100, choices=PodMode.choices, default=PodMode.PRODUCTION, help_text="The mode this pod is running in")
    strategy = models.CharField(max_length=100, default=PodStrategy.NODE, choices=PodStrategy.choices, help_text="The stragey of this pod")
    name = models.CharField(max_length=300, default=generate_random_name, help_text="A unique name for this pod")
    unique = models.UUIDField(max_length=1000, unique=True, default=uuid.uuid4, help_text="A Unique identifier for this Pod")
    channel = PodChannel(max_length=5000, help_text="The exclusive channel where the Pod listens to [depending on Stragey]", default=uuid.uuid4, unique=True)
    statusmessage = models.CharField(max_length=300, blank=True, help_text="This pods Status")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template","name"], name="A pod needs to uniquely identify with a name for a template")
        ]

    def __str__(self):
        return f"{self.template} - {self.status}"


class Commission(models.Model):

    pod = models.ForeignKey(Pod, on_delete=models.CASCADE, help_text="Which pod are we commisssioning?", related_name="commisions")
    reference = models.UUIDField(max_length=1000, unique=True, default=uuid.uuid4, help_text="A Unique identifier for this Commision")
    



class Reservation(models.Model):

    #1 Inputs to the the Provision (it can be either already a template to provision or just a node)
    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this reservation connects", related_name="reservations", null=True, blank=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The template this reservation connects", related_name="reservations", null=True, blank=True)
    pod = models.ForeignKey(Pod, on_delete=models.SET_NULL, help_text="The pod this reservation connects", related_name="reservations", null=True, blank=True)

    # Selection criteria for finding a right Channel
    params = models.JSONField(null=True, blank=True, help_text="Params for the Policy (including Provider etc..)") 

    # 2. The result (provider is stored already in pod, no need to)
    channel = models.CharField(max_length=6000)
    
    #Status Field
    status = models.CharField(max_length=300, choices=ReservationStatus.choices, default=ReservationStatus.PENDING.value, help_text="Current lifecycle of Provision")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Provision as for now", blank=True)

    #Callback
    callback =  models.CharField(max_length=1000, help_text="Callback", blank=True, null=True)
    progress =  models.CharField(max_length=1000, help_text="Provider", blank=True, null=True)

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Provisions parent", related_name="children")
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="This provision creator", null=True, blank=True)
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Assignation")


    def __str__(self):
        return f"Reservation for Node: {self.node.interface if self.node else ''} | Template: {self.template if self.template else ''} to {self.pod} Referenced {self.reference}"


class ReservationLog(models.Model):
    reservation = models.ForeignKey(Reservation, help_text="The reservation this log item belongs to", related_name="log", on_delete=models.CASCADE)
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(choices=ReservationLogLevel.choices, default=ReservationLogLevel.INFO.value, max_length=200)


class Assignation(models.Model):
    """ A constant log of a tasks transition through finding a Node, Template and finally Pod , also a store for its results"""
    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The Node this assignation is having", blank=True, null=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The Template this assignation is using", blank=True, null=True)
    pod = models.ForeignKey(Pod, on_delete=models.CASCADE, help_text="The pod this assignation connects to", related_name="assignations", blank=True, null=True)
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, help_text="Which reservation are we assigning to", related_name="assignations", blank=True, null=True)

    # 1. Input to the Assignation
    args = models.JSONField(blank=True, null=True, help_text="The Args")
    kwargs = models.JSONField(blank=True, null=True, help_text="The Kwargs")

    returns = models.JSONField(blank=True, null=True, help_text="The Returns")

    # 2. Outputs of the Assignation
    outputs = OutputsField(help_text="The Outputs", blank=True, null=True)

    status = models.CharField(max_length=300, choices=AssignationStatus.choices, default=AssignationStatus.PENDING.value, help_text="Current lifecycle of Assignation")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Assignation as for now", blank=True)
    
    # Callbacks are only to be set if there is a need to transvere through Django (e.g assignation through CHannels)
    # Callbacks (once the Task, has yielded, finished, errored or has been cancelled)
    # Progress (for progress reports if desired...)
    callback = models.CharField(max_length=1000, help_text="The Callback queue once the Assignation has finished", null=True, blank=True)
    progress = models.CharField(max_length=1000, help_text="The Progress queue once the Assignation has finished", null=True, blank=True)

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Assignation")
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="The creator is this assignation", null=True, blank=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Assignations parent", related_name="children")
   


class AssignationLog(models.Model):
    reservation = models.ForeignKey(Assignation, help_text="The reservation this log item belongs to", related_name="log", on_delete=models.CASCADE)
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(choices=AssignationLogLevel.choices, default=AssignationLogLevel.INFO.value, max_length=200)