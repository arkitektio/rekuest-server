from balder.enum import InputEnum
from django.db.models import TextChoices


class ProvisionMode(TextChoices):
    DEBUG = "DEBUG", "Debug Mode (Node might be constantly evolving)"
    PRODUCTION = "PRODUCTION", "Production Mode (Node might be constantly evolving)"


class LogLevel(TextChoices):
    CRITICAL = "CRITICAL", "CRITICAL Level"
    INFO = "INFO", "INFO Level"
    DEBUG = "DEBUG", "DEBUG Level"
    ERROR = "ERROR", "ERROR Level"
    WARN = "WARN", "WARN Level"
    YIELD = "YIELD", "YIELD Level"
    CANCEL = "CANCEL", "Cancel Level"
    RETURN = "RETURN", "YIELD Level"
    DONE = "DONE", "Done Level"
    EVENT = "EVENT", "Event Level (only handled by plugins)"


class RepositoryType(TextChoices):
    """Repository Types expresses what sort of Repository we are dealing with, e.g is this a local, mirror??"""

    APP = "app", "Repository that is hosted by an App"
    MIRROR = "mirror", "Repository mirrors online Repository"


class AccessStrategy(TextChoices):
    """How this Topic is accessible"""

    EXCLUSIVE = (
        "EXCLUSIVE",
        "This Topic is Only Accessible linkable for its creating User",
    )
    EVERYONE = "EVERYONE", "Everyone can link to this Topic"


class NodeType(TextChoices):
    GENERATOR = "generator", "Generator"
    FUNCTION = "function", "Function"


class TopicMode(TextChoices):
    PRODUCTION = "PRODUCTION", "Production (Topic is in production mode)"
    DEBUG = "DEBUG", "Debug (Topic is in debug Mode)"
    TEST = "TEST", "Test (is currently being tested)"


class AssignationStatus(TextChoices):
    PENDING = "PENDING", "Pending"  # Assignation has been requested
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"  # Assignation has been requested
    RETURNED = "RETURNED", "Assignation Returned (Only for Functions)"
    # Arnheim acknowledgments
    DENIED = "DENIED", "Denied (Assingment was rejected)"
    ASSIGNED = "ASSIGNED", "Was able to assign to a pod"

    # Progress reports
    PROGRESS = "PROGRESS", "Progress (Assignment has current Progress)"
    RECEIVED = "RECEIVED", "Received (Assignment was received by an agent)"

    # Unsuccessfull Termination
    ERROR = "ERROR", "Error (Retrieable)"
    CRITICAL = "CRITICAL", "Critical Error (No Retries available)"
    CANCEL = "CANCEL", "Assinment is beeing cancelled"
    CANCELING = "CANCELING", "Cancelling (Assingment is currently being cancelled)"
    CANCELLED = "CANCELLED", "Assignment has been cancelled."

    # Successfull Termination
    YIELD = "YIELD", "Assignment yielded a value (only for Generators)"
    DONE = "DONE", "Assignment has finished"


class ReservationStatus(TextChoices):

    # Start State
    ROUTING = (
        "ROUTING",
        "Routing (Reservation has been requested but no Topic found yet)",
    )
    NON_VIABLE = (
        "NON_VIABLE",
        "SHould signal that this reservation is non viable (has less linked provisions than minimalInstances)",
    )

    # Life States
    PROVIDING = (
        "PROVIDING",
        "Providing (Reservation required the provision of a new worker)",
    )
    WAITING = (
        "WAITING",
        "Waiting (We are waiting for any assignable Topic to come online)",
    )  # TODO: I s this actually a Double State

    REROUTING = (
        "REROUTING",
        "Rerouting (State of provisions this reservation connects to have changed and require Retouring)",
    )
    DISCONNECTED = (
        "DISCONNECTED",
        "Disconnect (State of provisions this reservation connects to have changed and require Retouring)",
    )
    DISCONNECT = (
        "DISCONNECT",
        "Disconnect (State of provisions this reservation connects to have changed and require Retouring)",
    )
    CANCELING = "CANCELING", "Cancelling (Reervation is currently being cancelled)"
    ACTIVE = "ACTIVE", "Active (Reservation is active and accepts assignments"

    # End States
    ERROR = (
        "ERROR",
        "Error (Reservation was not able to be performed (See StatusMessage)",
    )
    ENDED = (
        "ENDED",
        "Ended (Reservation was ended by the the Platform and is no longer active)",
    )
    CANCELLED = (
        "CANCELLED",
        "Cancelled (Reservation was cancelled by user and is no longer active)",
    )
    CRITICAL = "CRITICAL", "Critical (Reservation failed with an Critical Error)"


class AgentStatus(TextChoices):
    ACTIVE = "ACTIVE", "Active"
    DISCONNECTED = "DISCONNECTED", "Disconnected"
    VANILLA = "VANILLA", "Complete Vanilla Scenario after a forced restart of"


class WaiterStatus(TextChoices):
    ACTIVE = "ACTIVE", "Active"
    DISCONNECTED = "DISCONNECTED", "Disconnected"
    VANILLA = "VANILLA", "Complete Vanilla Scenario after a forced restart of"


class ProvisionStatus(TextChoices):

    # Start State
    PENDING = (
        "PENDING",
        "Pending (Request has been created and waits for its initial creation)",
    )
    BOUND = (
        "BOUND",
        "Bound (Provision was bound to an Agent)",
    )
    PROVIDING = (
        "PROVIDING",
        "Providing (Request has been send to its Agent and waits for Result",
    )

    # Life States
    ACTIVE = "ACTIVE", "Active (Provision is currently active)"
    INACTIVE = "INACTIVE", "Inactive (Provision is currently not active)"
    CANCELING = "CANCELING", "Cancelling (Provisions is currently being cancelled)"
    DISCONNECTED = "LOST", "Lost (Subscribers to this Topic have lost their connection)"
    RECONNECTING = (
        "RECONNECTING",
        "Reconnecting (We are trying to Reconnect to this Topic)",
    )

    # End States
    DENIED = "DENIED", "Denied (Provision was rejected for this User)"

    # End States
    ERROR = (
        "ERROR",
        "Error (Reservation was not able to be performed (See StatusMessage)",
    )
    CRITICAL = "CRITICAL", "Critical (Provision resulted in an critical system error)"
    ENDED = (
        "ENDED",
        "Ended (Provision was cancelled by the Platform and will no longer create Topics)",
    )
    CANCELLED = (
        "CANCELLED",
        "Cancelled (Provision was cancelled by the User and will no longer create Topics)",
    )
