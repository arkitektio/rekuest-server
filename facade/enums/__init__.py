"""GraphQL enums and Django ``TextChoices`` for the facade app.

Split into domain submodules. This ``__init__`` re-exports every public enum so
the established ``from facade import enums`` / ``enums.X`` access keeps working.
"""

from .action import ActionKindChoices, ActionScope, DemandKind, EffectsChoices, ExecutionChoices, HookKind
from .agent import (
    AgentEventChoices,
    AgentKind,
)
from .task import (
    TaskEventChoices,
    TaskEventKind,
    TaskInstructChoices,
    TaskInstructKind,
)
from .log import LogLevel, LogLevelChoices
from .signal import FiringOutcome, FiringOutcomeChoices, ScheduleOverlap, ScheduleOverlapChoices, SignalKind, SignalKindChoices
from .state import JSONPatchOperation

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
    # agent
    "AgentEventChoices",
    "AgentKind",
    # task
    "TaskEventChoices",
    "TaskEventKind",
    "TaskInstructChoices",
    "TaskInstructKind",
    # log
    "LogLevel",
    "LogLevelChoices",
    # state
    "JSONPatchOperation",
]
