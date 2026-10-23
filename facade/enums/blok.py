from enum import Enum

import strawberry
from django.db.models import TextChoices


class MaterializationScopeChoices(TextChoices):
    """Where a materialization lives (persisted on ``MaterializedBlok.scope``)."""

    DASHBOARD = "DASHBOARD", "Dashboard"
    MESSAGE = "MESSAGE", "Message"
    TASK = "TASK", "Task"


@strawberry.enum(description="Where a materialization lives: DASHBOARD ones are listed and placed on dashboards; a MESSAGE one belongs to the message it was made for, a TASK one to the task that showed it (to ask or to inform). Neither is listed; each is reached by its id.")
class MaterializationScope(str, Enum):
    DASHBOARD = "DASHBOARD"
    MESSAGE = "MESSAGE"
    TASK = "TASK"


class TaskBlokRoleChoices(TextChoices):
    """Why a task showed a blok (persisted on ``MaterializedBlok.task_role``)."""

    ASK = "ASK", "Ask"
    INFORM = "INFORM", "Inform"


@strawberry.enum(description="Why a task showed a blok: ASK holds the task until a person answers through it, INFORM shows it while the task carries on.")
class TaskBlokRole(str, Enum):
    ASK = "ASK"
    INFORM = "INFORM"


@strawberry.enum(description="Where a blok a task showed stands. WAITING: an ask nobody answered yet. ANSWERED / DISMISSED: a person answered it, or turned it down. GONE: the task ended before anyone did. SHOWN: an inform.")
class TaskBlokStatus(str, Enum):
    WAITING = "WAITING"
    ANSWERED = "ANSWERED"
    DISMISSED = "DISMISSED"
    GONE = "GONE"
    SHOWN = "SHOWN"
