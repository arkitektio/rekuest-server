from typing import Text
from django.db.models import TextChoices

class DataPointType(TextChoices):
    """ Variety expresses the Type of Representation we are dealing with
    """
    GRAPHQL = "graphql", "Graphql (Access through the GraphQL Interface or Ward)"
    REST = "rest", "Rest (Access through the GraphQL Interface or Ward)"


class RepositoryType(TextChoices):
    """ Repository Types expresses what sort of Repository we are dealing with, e.g is this a local, mirror??"""
    LOCAL = "local", "Repository only exists locally"
    MIRROR = "mirror", "Repository mirrors online"


class ClientType(TextChoices):
    HOST = "Host", "Hosting Client"
    CLIENT = "Client", "Client client" 



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