from delt.messages.generics import Context
from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from herre.models import HerreApp
from mars.names import generate_random_name
from facade.fields import ArgsField, KwargsField, OutputsField, ParamsField, PodChannel, ReturnField
from facade.enums import AccessStrategy, LogLevel, AssignationStatus, DataPointType, LogLevel, NodeType,  ProvisionStatus,  ReservationStatus, TopicStatus
from django.db import models
from django.contrib.auth import get_user_model
import uuid
import logging

logger = logging.getLogger(__name__)



def create_token(user, scopes=[]):
    return Context(**{"roles": user.roles, "scopes": scopes, "user": user.email})

class DataPoint(models.Model):
    """A Datapoint constitues Arkitekts Representation of a Host of Data.

    Datapoints host Datamodels, that in turn are accessible as Models for Node inputs and node outputs

    """
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, help_text="The Associated App")
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, blank=True, help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users", null=True)
    version = models.CharField(max_length=100, help_text="The version of the bergen API this endpoint uses")
    inward = models.CharField(max_length=100, help_text="Inward facing hostname (for Docker powered access)", null=True, blank=True)
    outward = models.CharField(max_length=100, help_text="Outward facing hostname for external clients", null=True, blank=True)
    port = models.IntegerField(help_text="Listening port", null=True, blank=True)
    type = models.CharField(max_length=100, choices=DataPointType.choices, default=DataPointType.GRAPHQL, help_text="The type of datapoint")
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    needs_negotiation = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app","user"], name="No multiple AppPoints for same App and User allowed")
        ]

    def create_ward(self, internal=True):
        return {"distinct": self.app.name, "host": self.inward if internal else self.outward, "port": self.port, "needsNegotiation": self.needs_negotiation, "type": self.type}

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
        return f"{self.name} - {self.package}/{self.interface}"



class Template(models.Model):
    """ A Template is a conceptual implementation of A Node. It represents its implementation as well as its performance"""

    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this template is implementatig", related_name="templates")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, help_text="The associated provider for this Template", related_name="templates")
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


class ProvisionLog(models.Model):
    provision = models.ForeignKey("Provision", help_text="The provision this log item belongs to", related_name="log", on_delete=models.CASCADE)
    message = models.CharField(max_length=20000, null=True, blank=True)
    level = models.CharField(choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200)


class Provision(models.Model):
    """ Topic (STATEFUL MODEL)
    
    Topic represents the current state of active Topics that are caused by provisions, they store the reservations (links)
    and are indexed by provision, a consumer connects through its provision to Arkitekt and sets the Topic to active, every
    reservation that is connected gets signalled that this Topic is now active. On disconnect every reservation can design
    according to its policy if it wants to wait for reconnect (if connection was Lost), raise an error, or choose another Topic.
    
    """

    unique = models.UUIDField(max_length=1000, unique=True, default=uuid.uuid4, help_text="A Unique identifier for this Topic")

    # Identifiers
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Provision")
    
    reservation = models.ForeignKey("Reservation", on_delete=models.CASCADE, null=True, blank=True, help_text="Reservation that created this provision (if we were auto created)", related_name="created_provisions")
    provision = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="Provision that created this provision (if we were auto created)", related_name="created_provisions")
    
    # Input
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The Template for this Provision", related_name="provisions", null=True, blank=True)
    
    # Platform specific Details (non relational Data)
    params = models.JSONField(null=True, blank=True, help_text="Params for the Policy (including Provider etc..)") 
    extensions = models.JSONField(null=True, blank=True, help_text="The Platform extensions")
    context = models.JSONField(null=True, blank=True, help_text="The Platform context")

    #
    access = models.CharField(max_length=100, default=AccessStrategy.EVERYONE, choices=AccessStrategy.choices, help_text="Access Strategy for this Provision")

    #Status Field
    status = models.CharField(max_length=300, choices=ProvisionStatus.choices, default=ProvisionStatus.PENDING.value, help_text="Current lifecycle of Provision")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Provision as for now", blank=True)

    #Callback
    callback =  models.CharField(max_length=1000, help_text="Callback", blank=True , null=True)
    progress =  models.CharField(max_length=1000, help_text="Provider", blank=True , null=True)

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="This provision creator", null=True, blank=True)
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, max_length=1000, help_text="This provision creator", null=True, blank=True)
    

    def __str__(self):
        return f"Provision for Template: {self.template if self.template else ''} Referenced {self.reference} || Reserved {self.reservation if self.reservation else 'without Reservation'}"


    def to_message(self) -> BouncedProvideMessage:
        return BouncedProvideMessage(data= {
            "template": self.template.id,
            "params": self.params
        }, meta= {
            "reference": self.reference,
            "extensions": self.extensions,
            "context": self.context
        })



class ReservationLog(models.Model):
    reservation = models.ForeignKey("Reservation", help_text="The reservation this log item belongs to", related_name="log", on_delete=models.CASCADE)
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200)



class Reservation(models.Model):
    """Reservation (CONTRACT MODEL)

    Reflects RabbitMQ Channel

    Reservations are constant logs of active connections to Arkitekt and are logging the state of the connection to the workers. They are user facing
    and are created by the user, they hold a log of all transactions that have been done since its inception, as well as as of the inputs that it was
    created by (Node and Template as desired inputs) and the currently active Topics it connects to. It also specifies the routing policy (it case a
    connection to a worker/app gets lost). A Reservation creates also a (rabbitmq) Channel that every connected Topic listens to and the specific user assigns to.
    According to its Routing Policy, if a Topic dies another Topic can eithers take over and get the Items stored in this  (rabbitmq) Channel or a specific user  event
    happens with this Assignations.

    """

    # Channel is the RabbitMQ channel that every user assigns to and that every topic listens to 
    channel = models.CharField(max_length=2000, unique=True, default=uuid.uuid4, help_text="The channel of this Reservation")


    #1 Inputs to the the Reservation (it can be either already a template to provision or just a node)
    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this reservation connects", related_name="reservations", null=True, blank=True)
    title = models.CharField(max_length=200, help_text="A Short Hand Way to identify this reservation for you", null=True, blank=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The template this reservation connects", related_name="reservations", null=True, blank=True)

    # The connections
    provisions = models.ManyToManyField(Provision, help_text="The Provisions this reservation connects", related_name="reservations", null=True, blank=True)
    
    # Platform specific Details (non relational Data)
    params = models.JSONField(default=dict,help_text="Params for the Policy (including Provider etc..)") 
    extensions = models.JSONField(default=dict, help_text="The Platform extensions")
    context = models.JSONField(default=dict,help_text="The Platform context")

    #Status Field
    status = models.CharField(max_length=300, choices=ReservationStatus.choices, default=ReservationStatus.ACTIVE, help_text="Current lifecycle of Reservation")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Provision as for now", blank=True)

    #Callback
    callback =  models.CharField(max_length=1000, help_text="Callback", blank=True, null=True)
    progress =  models.CharField(max_length=1000, help_text="Provider", blank=True, null=True)

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, max_length=1000, help_text="This Reservations app", null=True, blank=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Provisions parent", related_name="children")
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="This Reservations creator", null=True, blank=True)
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Assignation")
    causing_provision = models.ForeignKey("Provision", on_delete=models.CASCADE, null=True, blank=True, help_text="Was this Reservation caused by a Provision", related_name="caused_reservations")


    def log(self, message, level=LogLevel.DEBUG):
        return ReservationLog.objects.create(message=message, reservation=self, level=level)

    def __str__(self):
        return f"Res for Node {self.node}" if self.node else f"Res for Template {self.template}"


class Assignation(models.Model):
    """ A constant log of a tasks transition through finding a Node, Template and finally Pod , also a store for its results"""
    reservation = models.ForeignKey(Reservation, on_delete=models.CASCADE, help_text="Which reservation are we assigning to", related_name="assignations", blank=True, null=True)
    extensions = models.JSONField(default=dict, help_text="The Platform extensions")
    context = models.JSONField(default=dict,help_text="The Platform context")

    # 1. The State of Everything
    args = models.JSONField(blank=True, null=True, help_text="The Args")
    kwargs = models.JSONField(blank=True, null=True, help_text="The Kwargs")
    returns = models.JSONField(blank=True, null=True, help_text="The Returns")
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
    app = models.ForeignKey(HerreApp, on_delete=models.CASCADE, max_length=1000, help_text="The app is this assignation", null=True, blank=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Assignations parent", related_name="children")


    def __str__(self):
        return f'{self.status} for {self.reservation}'
   


class AssignationLog(models.Model):
    reservation = models.ForeignKey(Assignation, help_text="The reservation this log item belongs to", related_name="log", on_delete=models.CASCADE)
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200)

