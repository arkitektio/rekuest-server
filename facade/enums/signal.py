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
