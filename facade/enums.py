from logging import WARN
from balder.enum import InputEnum
from typing import Text
from django.db.models import TextChoices

class DataPointType(TextChoices):
    """ Variety expresses the Type of Representation we are dealing with
    """
    GRAPHQL = "graphql", "Graphql (Access through the GraphQL Interface or Ward)"
    REST = "rest", "Rest (Access through the GraphQL Interface or Ward)"


class HookType(TextChoices):
    NEGOTIATE = "negotiate", "Negotiate Hook (syncronous Api call)"


class LogLevel(TextChoices):
    CRITICAL = "CRITICAL", "CRITICAL Level"
    INFO = "INFO", "INFO Level"
    DEBUG = "DEBUG", "DEBUG Level"    
    ERROR = "ERROR", "ERROR Level"   
    WARN = "WARN", "WARN Level"   

LogLevelInput = InputEnum.from_choices(LogLevel)


class RepositoryType(TextChoices):
    """ Repository Types expresses what sort of Repository we are dealing with, e.g is this a local, mirror??"""
    APP = "app", "Repository that is hosted by an App"
    MIRROR = "mirror", "Repository mirrors online Repository"


class AccessStrategy(TextChoices):
    """How this Topic is accessible """
    EXCLUSIVE = "EXCLUSIVE", "This Topic is Only Accessible linkable for its creating User"
    EVERYONE = "EVERYONE", "Everyone can link to this Topic"


class ClientType(TextChoices):
    HOST = "Host", "Hosting Client"
    CLIENT = "Client", "Client client"
    PROVIDER = "Provider", "Providing client" 
    POINT = "Point", "Hosts Datamodels"


class NodeType(TextChoices):
    GENERATOR = "generator", "Generator"
    FUNCTION = "function", "Function"


class TopicMode(TextChoices):
    PRODUCTION = "PRODUCTION", "Production (Topic is in production mode)"
    DEBUG = "DEBUG", "Debug (Topic is in debug Mode)"
    TEST = "TEST", "Test (is currently being tested)"


class PodStatus(TextChoices):
    DOWN = "DOWN", "Down"
    ERROR = "ERROR", "Error"
    PENDING = "PENDING", "Pending"
    ACTIVE = "ACTIVE", "Active"

class TopicStatus(TextChoices):
    DOWN = "DOWN", "Down (Subscribers to this Topic are offline)"
    DISCONNECTED = "LOST", "Lost (Subscribers to this Topic have lost their connection)"
    RECONNECTING = "RECONNECTING", "Reconnecting (We are trying to Reconnect to this Topic)"
    CRITICAL = "CRITICAL", "Criticial (This Topic has errored and needs to be inspected)"
    ACTIVE = "ACTIVE", "Active (This topic has subscribers and is available being Routed To"


class AssignationStatus(TextChoices):
    PENDING = "PENDING", "Pending" # Assignation has been requested
    RETURNED = "RETURNED", "Assignation Returned (Only for Functions)"
    # Arnheim acknowledgments
    DENIED = "DENIED", "Denied (Assingment was rejected)"
    ASSIGNED = "ASSIGNED", "Was able to assign to a pod"
    
    # Progress reports
    PROGRESS = "PROGRESS", "Progress (Assignment has current Progress)"

    # Unsuccessfull Termination
    ERROR = "ERROR", "Error (Retrieable)"
    CRITICAL = "CRITICAL", "Critical Error (No Retries available)"
    CANCEL = "CANCEL", "Assinment is beeing cancelled"
    CANCELING = "CANCELING", "Cancelling (Assingment is currently being cancelled)"
    CANCELLED = "CANCELLED", "Assignment has been cancelled."

    # Successfull Termination
    YIELD = "YIELD", "Assignment yielded a value (only for Generators)"
    DONE = "DONE", "Assignment has finished"



AssignationStatusInput = InputEnum.from_choices(AssignationStatus)
NodeTypeInput = InputEnum.from_choices(NodeType)


class ReservationStatus(TextChoices):

    #Start State
    ROUTING = "ROUTING", "Routing (Reservation has been requested but no Topic found yet)"

    # Life States
    PROVIDING = "PROVIDING", "Providing (Reservation required the provision of a new worker)"
    WAITING = "WAITING", "Waiting (We are waiting for any assignable Topic to come online)" #TODO: I s this actually a Double State


    REROUTING = "REROUTING", "Rerouting (State of provisions this reservation connects to have changed and require Retouring)"
    DISCONNECTED = "DISCONNECTED", "Disconnect (State of provisions this reservation connects to have changed and require Retouring)"
    DISCONNECT = "DISCONNECT", "Disconnect (State of provisions this reservation connects to have changed and require Retouring)"
    CANCELING = "CANCELING", "Cancelling (Reervation is currently being cancelled)"
    ACTIVE = "ACTIVE", "Active (Reservation is active and accepts assignments"

    # End States
    ERROR = "ERROR", "Error (Reservation was not able to be performed (See StatusMessage)"
    ENDED = "ENDED", "Ended (Reservation was ended by the the Platform and is no longer active)"
    CANCELLED = "CANCELLED", "Cancelled (Reservation was cancelled by user and is no longer active)"
    CRITICAL = "CRITICAL", "Critical (Reservation failed with an Critical Error)"


ReservationStatusInput = InputEnum.from_choices(ReservationStatus)

class ProvisionStatus(TextChoices):

    # Start State
    PENDING = "PENDING", "Pending (Request has been created and waits for its initial creation)"
    PROVIDING = "PROVIDING", "Providing (Request has been send to its Provider and waits for Result"
    
    # Life States
    ACTIVE = "ACTIVE", "Active (Provision is currently active)"
    INACTIVE = "INACTIVE", "Inactive (Provision is currently not active)"
    CANCELING = "CANCELING", "Cancelling (Provisions is currently being cancelled)"
    DISCONNECTED = "LOST", "Lost (Subscribers to this Topic have lost their connection)"
    RECONNECTING = "RECONNECTING", "Reconnecting (We are trying to Reconnect to this Topic)"
    
    # End States
    DENIED = "DENIED", "Denied (Provision was rejected for this User)"

    # End States
    ERROR = "ERROR", "Error (Reservation was not able to be performed (See StatusMessage)"
    CRITICAL = "CRITICAL", "Critical (Provision resulted in an critical system error)"
    ENDED = "ENDED", "Ended (Provision was cancelled by the Platform and will no longer create Topics)"
    CANCELLED = "CANCELLED", "Cancelled (Provision was cancelled by the User and will no longer create Topics)"

    

ProvisionStatusInput = InputEnum.from_choices(ProvisionStatus)