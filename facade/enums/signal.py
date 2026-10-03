from enum import Enum

import strawberry
from django.db.models import TextChoices


class SignalKindChoices(TextChoices):
    """What happened to the object a service signalled (persisted on ``Signal.kind``/``Trigger.kind``)."""

    CREATED = "CREATED", "Created"
    UPDATED = "UPDATED", "Updated"
    DELETED = "DELETED", "Deleted"


@strawberry.enum(description="What happened to the object a service signalled.")
class SignalKind(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"


class FiringOutcomeChoices(TextChoices):
    """What became of one trigger for one signal (persisted on ``Firing.outcome``)."""

    FIRED = "FIRED", "Fired"
    REJECTED = "REJECTED", "Rejected"
    FAILED = "FAILED", "Failed"


@strawberry.enum(description="What became of one trigger for one signal: it fired a run, it was rejected (the signal did not satisfy it, or a policy held it back), or creating the run failed.")
class FiringOutcome(str, Enum):
    FIRED = "FIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ScheduleOverlapChoices(TextChoices):
    """Whether a schedule's run may start while its previous one is still open (persisted on ``Schedule.overlap``)."""

    SKIP = "SKIP", "Skip"
    ALLOW = "ALLOW", "Allow"


@strawberry.enum(description="Whether a schedule's run may start while its previous one is still open. SKIP: the next run is planned once the previous finished. ALLOW: it is planned as soon as the previous was handed over, so runs may overlap.")
class ScheduleOverlap(str, Enum):
    SKIP = "SKIP"
    ALLOW = "ALLOW"
