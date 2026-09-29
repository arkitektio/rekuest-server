from enum import Enum

import strawberry
from django.db.models import TextChoices


class ActionKindChoices(TextChoices):
    FUNCTION = "FUNCTION", "Function"
    GENERATOR = "GENERATOR", "Generator"


class EffectsChoices(TextChoices):
    """What running an implementation again would do (persisted on ``Implementation.effects``).

    Purely informational: it is shown to whoever decides about a lost task, and never
    decides what the server does, with one exception: the progress lease watches
    IRREVERSIBLE work.
    """

    NONE = "NONE", "None (changes nothing)"
    REPEATABLE = "REPEATABLE", "Repeatable (a second run leaves the same state)"
    UNKNOWN = "UNKNOWN", "Unknown (no claim)"
    IRREVERSIBLE = "IRREVERSIBLE", "Irreversible (a second run happens again, in the real world)"


class ExecutionChoices(TextChoices):
    """How an implementation runs (persisted on ``Implementation.execution``).

    A WORKFLOW may call other actions and is resumed from its journal when its agent
    dies; a PLAIN implementation's task ends LOST.
    """

    PLAIN = "PLAIN", "Plain"
    WORKFLOW = "WORKFLOW", "Workflow (resumed from its journal)"


@strawberry.enum
class ActionScope(str, Enum):
    GLOBAL = "GLOBAL"
    LOCAL = "LOCAL"
    BRIDGE_GLOBAL_TO_LOCAL = "BRIDGE_GLOBAL_TO_LOCAL"
    BRIDGE_LOCAL_TO_GLOBAL = "BRIDGE_LOCAL_TO_GLOBAL"


@strawberry.enum
class DemandKind(str, Enum):
    ARGS = "args"
    RETURNS = "returns"


@strawberry.enum
class HookKind(str, Enum):
    CLEANUP = "CLEANUP"
    INIT = "INIT"
