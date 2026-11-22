"""GraphQL enums and Django ``TextChoices`` for the facade app.

Split into domain submodules. This ``__init__`` re-exports every public enum so
the established ``from facade import enums`` / ``enums.X`` access keeps working.
"""

from .action import (
    ActionAvailability,
    ActionKindChoices,
    ActionScope,
    DemandKind,
    EffectsChoices,
    ExecutionChoices,
    HookKind,
)
from .agent import (
    AgentEventChoices,
    AgentKind,
)
from .blok import (
    MaterializationScope,
    MaterializationScopeChoices,
    TaskBlokRole,
    TaskBlokRoleChoices,
    TaskBlokStatus,
)
from .log import LogLevel, LogLevelChoices
from .signal import (
    FiringOutcome,
    FiringOutcomeChoices,
    ScheduleOverlap,
    ScheduleOverlapChoices,
    SignalKind,
    SignalKindChoices,
)
from .state import JSONPatchOperation
from .task import (
    HoldRemember,
    OnLost,
    TaskEventChoices,
    TaskEventKind,
    TaskInstructChoices,
    TaskInstructKind,
    TaskPolicy,
    TaskPolicyChoices,
    TaskStatus,
)

__all__ = [
    # signal
    "SignalKind",
    "SignalKindChoices",
    "FiringOutcome",
    "FiringOutcomeChoices",
    "ScheduleOverlap",
    "ScheduleOverlapChoices",
    # action
    "ActionKindChoices",
    "ActionScope",
    "DemandKind",
    "EffectsChoices",
    "ExecutionChoices",
    "HookKind",
    # blok
    "MaterializationScope",
    "MaterializationScopeChoices",
    "TaskBlokRole",
    "TaskBlokRoleChoices",
    "TaskBlokStatus",
    # agent
    "AgentEventChoices",
    "AgentKind",
    # task
    "TaskEventChoices",
    "TaskEventKind",
    "TaskInstructChoices",
    "TaskInstructKind",
    "HoldRemember",
    "TaskStatus",
    "OnLost",
    "TaskPolicy",
    "TaskPolicyChoices",
    # log
    "LogLevel",
    "LogLevelChoices",
    # state
    "JSONPatchOperation",
]
