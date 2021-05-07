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
    INFO = "INFO", "INFO Level"
    DEBUG = "DEBUG", "DEBUG Level"    
    ERROR = "ERROR", "ERROR Level"    

class ReservationLogLevel(TextChoices):
    INFO = "INFO", "INFO Level"    
    DEBUG = "DEBUG", "DEBUG Level"    
    ERROR = "ERROR", "ERROR Level"    

class AssignationLogLevel(TextChoices):
    INFO = "INFO", "INFO Level"    
    DEBUG = "DEBUG", "DEBUG Level"    
    ERROR = "ERROR", "ERROR Level"    


class RepositoryType(TextChoices):
    """ Repository Types expresses what sort of Repository we are dealing with, e.g is this a local, mirror??"""
    APP = "app", "Repository that is hosted by an App"
    MIRROR = "mirror", "Repository mirrors online Repository"


class PodStrategy(TextChoices):
    """ How this pod listens to events """
    EXCLUSIVE = "exclusive", "Pod listens only to event that are happening through provisions"
    TEMPLATE = "template", "Pod listens too template events"
    NODE = "node", "Pod listens too Node assignation events"


class ClientType(TextChoices):
    HOST = "Host", "Hosting Client"
    CLIENT = "Client", "Client client"
    PROVIDER = "Provider", "Providing client" 
    POINT = "Point", "Hosts Datamodels"



class NodeType(TextChoices):
    GENERATOR = "generator", "Generator"
    FUNCTION = "function", "Function"



class PodMode(TextChoices):
    PRODUCTION = "producton", "Production (runs in)"
    DEBUG = "debug", "Debug (Pod is currently debugging)"
    TEST = "test", "Pod is currently being tested"



class PodStatus(TextChoices):
    DOWN = "DOWN", "Down"
    ERROR = "ERROR", "Error"
    PENDING = "PENDING", "Pending"
    ACTIVE = "ACTIVE", "Active"



class AssignationStatus(TextChoices):
    PENDING = "PENDING", "Pending" # Assignation has been requested

    # Arnheim acknowledgments
    DENIED = "DENIED", "Denied (Assingment was rejected)"
    ASSIGNED = "ASSIGNED", "Was able to assign to a pod"
    
    # Progress reports
    PROGRESS = "PROGRESS", "Progress (Assignment has current Progress)"

    # Unsuccessfull Termination
    ERROR = "ERROR", "Error (Retrieable)"
    CRITICAL = "CRITICAL", "Critical Error (No Retries available)"
    CANCEL = "CANCEL", "Assinment is beeing cancelled"
    CANCELLED = "CANCELLED", "Assignment has been cancelled."

    # Successfull Termination
    YIELD = "YIELD", "Assignment yielded a value (only for Generators)"
    DONE = "DONE", "Assignment has finished"



AssignationStatusInput = InputEnum.from_choices(AssignationStatus)


class ReservationStatus(TextChoices):
    PENDING = "PENDING", "Pending" # Assignation has been requested
    PROVIDING = "PROVIDING", "Providing"
    # Arnheim acknowledgments
    DENIED = "DENIED", "Denied (Provision was rejected)"
    ENDED = "ENDED", "Reservation has finished and is no longer active"
    
    # Progress reports
    PROGRESS = "PROGRESS", "Progress (Provision has current Progress)"

    # Unsuccessfull Termination
    ERROR = "ERROR", "Error (Retrieable)"
    CRITICAL = "CRITICAL", "Critical Error (No Retries available)"
    CANCEL = "CANCEL", "Provision is beeing cancelled"
    CANCELLED = "CANCELLED", "Provision has been cancelled."

    # Successfull Termination
    DONE = "DONE", "Reservation is active and assignable"



class ProvisionStatus(TextChoices):
    PENDING = "PENDING", "Pending" # Assignation has been requested

    # Arnheim acknowledgments
    DENIED = "DENIED", "Denied (Provision was rejected)"
    
    # Progress reports
    PROGRESS = "PROGRESS", "Progress (Provision has current Progress)"

    # Unsuccessfull Termination
    ERROR = "ERROR", "Error (Retrieable)"
    CRITICAL = "CRITICAL", "Critical Error (No Retries available)"
    CANCEL = "CANCEL", "Provision is beeing cancelled"
    CANCELLED = "CANCELLED", "Provision has been cancelled."

    # Successfull Termination
    DONE = "DONE", "Provision has finished (pod is available and will connect)"