from enum import Enum

import strawberry
from django.db.models import TextChoices


class TaskEventChoices(TextChoices):
    """These are the possible events that can happen to a Task.

    The events are ordered by their appearance in the lifecycle of a Task.


    """

    QUEUED = "QUEUED", "Queued (Task was queued)"
    BOUND = "BOUND", "Bound (Task was bound to an Agent)"
    STARTED = "STARTED", "Started (Agent accepted and began the Task)"
    UNASSIGN = "UNASSIGN", "Unassign (Agent received the Task)"
    PROGRESS = "PROGRESS", "Progress (Agent is making progress on the Task)"
    CANCELLING = "CANCELLING", "Cancelling (a cancel was requested; awaiting the agent's confirmation)"
    CANCELLED = "CANCELLED", "Cancelled (the agent cancelled the task)"
    INTERRUPTING = "INTERRUPTING", "Interrupting (an interrupt was requested; awaiting the agent's confirmation)"
    INTERRUPTED = "INTERRUPTED", "Interrupted (the agent interrupted the task)"
    PAUSING = "PAUSING", "Pausing (a pause was requested; awaiting the agent's confirmation)"
    PAUSED = "PAUSED", "Paused (the agent suspended the task)"
    RESUMING = "RESUMING", "Resuming (a resume was requested; awaiting the agent's confirmation)"
    RESUMED = "RESUMED", "Resumed (the agent resumed the task)"
    DELEGATE = "DELEGATE", "Delegate (Task was delegated to another Task)"
    FAILED = "FAILED"
    CRITICAL = "CRITICAL"
    DISCONNECTED = "DISCONNECTED"

    YIELD = (
        "YIELD",
        "Yields (Agent yielded the result)",
    )  # One yield can be interpreted as a return
    COMPLETED = "COMPLETED", "Completed (Agent finished the Task)"
    LOG = "LOG", "Log (Agent logged a message)"
    EFFECT = "EFFECT", "Effect (the task took a value from outside itself: the clock, randomness, a sleep deadline)"


class TaskInstructChoices(TextChoices):
    """These are the possible events that are instructed to a Task."""

    ASSIGN = "ASSIGN", "Assign (Agent accepted the Task)"
    CANCEL = "CANCEL", "Unassign (Agent received the Task)"
    RESUME = "RESUME", "Resume (Agent resumed the Task)"
    PAUSE = "PAUSE", "Pause (Agent paused the Task)"
    INTERRUPT = "INTERRUPT", "Interrupt (Agent interupted the Task)"
    COLLECT = "COLLECT", "Collect instruction received"


@strawberry.enum(description="The event kind of the taskevent")
class TaskInstructKind(str, Enum):
    """These are the possible events that are instructed to a Task."""

    ASSIGN = "ASSIGN"
    CANCEL = "CANCEL"
    RESUME = "RESUME"
    PAUSE = "PAUSE"
    INTERRUPT = "INTERRUPT"
    COLLECT = "COLLECT"


@strawberry.enum(description="The event kind of the taskevent")
class TaskEventKind(str, Enum):
    """These are the possible events that can happen to a Task.

    The events are ordered by their appearance in the lifecycle of a Task.
    """

    BOUND = "BOUND"
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    DELEGATE = "DELEGATE"
    # Mirrors TaskEventChoices.UNASSIGN. Without it, coercing a persisted UNASSIGN row in the
    # task subscriptions raises ValueError.
    UNASSIGN = "UNASSIGN"

    DISCONNECTED = "DISCONNECTED"

    YIELD = "YIELD"
    COMPLETED = "COMPLETED"

    # Log Events
    LOG = "LOG"
    # A value the task took from outside itself, recorded for replay
    EFFECT = "EFFECT"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    INTERRUPTING = "INTERRUPTING"
    INTERRUPTED = "INTERRUPTED"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    RESUMED = "RESUMED"
    FAILED = "FAILED"
    CRITICAL = "CRITICAL"
