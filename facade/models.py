from django.db.models import constraints
from herre.token import JwtToken
from mars.names import generate_random_name
from facade.fields import InPortsField, InputsField, OutPortsField, OutputsField, ParamsField, PodChannel
from django.db.models.fields import NullBooleanField
from facade.enums import AssignationStatus, DataPointType, NodeType, PodMode, PodStatus, ProvisionStatus, RepositoryType
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
    version = models.CharField(max_length=100, help_text="The version of the bergen API this endpoint uses")
    inward = models.CharField(max_length=100, help_text="Inward facing hostname (for Docker powered access)")
    outward = models.CharField(max_length=100, help_text="Outward facing hostname for external clients")
    port = models.IntegerField(help_text="Listening port")
    type = models.CharField(max_length=100, choices=DataPointType.choices, default=DataPointType.GRAPHQL, help_text="The type of datapoint")
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["inward","port"], name="unique datapoint")
        ]


    def negotiate(self, token: JwtToken):
        try:
            result = requests.get(f"http://{self.inward}:{self.port}/.well-known/extensions", headers={"AUTHORIZATION": f"Bearer {token.token}"})
            return result.json()
        except Exception as e:
            logger.error(e)
            return [] 





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



class Repository(models.Model):
    """ A Repository is the housing conatinaer for a Node, Multiple Nodes belong to one repository.

    Repositories can be replicas of online sources (think pypi repository), but also containers for
    local user generated nodes (think nodes that were generated through the flow provider)
    """
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, null=True, blank=True, help_text="The Person that created this repository")
    name = models.CharField(max_length=1000, help_text="The name of this Repository")
    type = models.CharField(max_length=200,choices=RepositoryType.choices, default=RepositoryType.LOCAL, help_text="Type of repository")


class Provider(models.Model):
    """ A provider is the intermediate step from a template to a pod, it takes an associated template
    and transfers it to a pod, given the current restrictions of the setup"""
    app = models.CharField(max_length=600, help_text="Do we have an external client? The Client ID of the App connecting, Default to internal if this is internal", default=uuid.uuid4) 
    user = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users", null=True)
    name = models.CharField(max_length=2000, help_text="This providers Name", default="Nana")    
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(max_length=1000, default=uuid.uuid4)
    internal = models.BooleanField(default=False)
    active = models.BooleanField(default=False, help_text="Is this Provider active right now?")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app","user"], name="No multiple Providers for same App and User allowed")
        ]

    def __str__(self):
        return f"{self.name} for {self.user}"



class Node(models.Model):
    """ Nodes are abstraction of RPC Tasks. They provide a common API to deal with creating tasks.

    See online Documentation"""
    type = models.CharField(max_length=1000, choices=NodeType.choices, default=NodeType.FUNCTION, help_text="Function, generator? Check async Programming Textbook")
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="nodes")


    name = models.CharField(max_length=1000, help_text="The cleartext name of this Node")
    package = models.CharField(max_length=1000, help_text="Package (think Module)")
    interface = models.CharField(max_length=1000, help_text="Interface (think Function)")

    description = models.TextField(help_text="A description for the Node")
    image = models.ImageField(null=True, blank=True, help_text="A short description what this Node does")
    inputs = InPortsField(default=list, help_text="Inputs for this Node")
    outputs = OutPortsField(default=list, help_text="Outputs for this Node")
    
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository","package","interface"], name="package, interface, repository cannot be the same")
        ]

    def __str__(self):
        return f"Node: {self.name} - {self.package}/{self.interface}"


class Template(models.Model):
    """ A Template is a conceptual implementation of A Node. It represents its implementation as well as its performance"""

    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this template is implementatig")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, help_text="The associated provider for this Template")
    name = models.CharField(max_length=1000, default=generate_random_name, help_text="A name for this Template")

    params = ParamsField(default=dict, help_text="Params for this Template")

    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, null=True, help_text="Who created this template on this instance")
    version = models.CharField(max_length=400, help_text="A short descriptor for the kind of version") #Subject to change
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["node","params"], name="A template has unique params for every node")
        ]

    def __str__(self):
        return f"{self.node} implemented by {self.provider}"



class Pod(models.Model):
    """ The last step in any provision, pods are running implementations of templates (think workers)"""

    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The template that created this pod", related_name="pods")
    status = models.CharField(max_length=300, choices=PodStatus.choices, default=PodStatus.PENDING, help_text="Which lifecycle moment is this pod in")
    mode = models.CharField(max_length=100, choices=PodMode.choices, default=PodMode.PRODUCTION, help_text="The mode this pod is running in")

    name = models.CharField(max_length=300, default=generate_random_name, help_text="A unique name for this pod")
    unique = models.UUIDField(max_length=1000, unique=True, default=uuid.uuid4, help_text="A Unique identifier for this Pod")
    channel = PodChannel(max_length=5000, help_text="The channel where the Pod listens to (is null if no listener)", null=True, blank=True)
    statusmessage = models.CharField(max_length=300, blank=True, help_text="This pods Status")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template","name"], name="A pod needs to uniquely identify with a name for a template")
        ]

    def __str__(self):
        return f"{self.template} - {self.status}"
    


class Assignation(models.Model):
    """ A constant log of a tasks transition through finding a Node, Template and finally Pod , also a store for its results"""
    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The Node this assignation is having", blank=True, null=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The Template this assignation is using", blank=True, null=True)
    pod = models.ForeignKey(Pod, on_delete=models.CASCADE, help_text="The pod this assignation connects to", related_name="assignations", blank=True, null=True)

    # 1. Input to the Assignation
    inputs = InputsField(blank=True, null=True, help_text="The Inputs")

    # 2. Outputs of the Assignation
    outputs = OutputsField(help_text="The Outputs", blank=True, null=True)

    status = models.CharField(max_length=300, choices=AssignationStatus.choices, default=AssignationStatus.PENDING, help_text="Current lifecycle of Assignation")
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
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="The creator is this assignation")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Assignations parent", related_name="children")
   

class Provision(models.Model):

    #1 Inputs to the the Provision (it can be either already a template to provision or just a node)
    node = models.ForeignKey(Node, on_delete=models.CASCADE, help_text="The node this provision connects", related_name="provisions", null=True, blank=True)
    template = models.ForeignKey(Template, on_delete=models.CASCADE, help_text="The node this provision connects", related_name="provisions", null=True, blank=True)
    
    # Selection criteria for finding a right Pod
    params = models.JSONField(null=True, blank=True, help_text="Params for the Policy (including Provider etc..)") 

    # 2. The result (provider is stored already in pod, no need to)
    pod = models.ForeignKey(Pod, on_delete=models.CASCADE, help_text="The pod this provision connects", related_name="provisions", null=True, blank=True)
    
    #Status Field
    status = models.CharField(max_length=300, choices=ProvisionStatus.choices, default=ProvisionStatus.PENDING, help_text="Current lifecycle of Provision")
    statusmessage = models.CharField(max_length=1000, help_text="Clear Text status of the Provision as for now", blank=True)

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, help_text="The Provisions parent", related_name="children")
    creator = models.ForeignKey(get_user_model(), on_delete=models.CASCADE, max_length=1000, help_text="This provision creator")
    reference = models.CharField(max_length=1000, unique=True, default=uuid.uuid4, help_text="The Unique identifier of this Assignation")



