"""GraphQL enums and Django ``TextChoices`` for the facade app.

Split into domain submodules. This ``__init__`` re-exports every public enum so
the established ``from facade import enums`` / ``enums.X`` access keeps working.
"""

from .action import ActionKindChoices, ActionScope, DemandKind, EffectsChoices, ExecutionChoices, HookKind
from .agent import (
    AgentEventChoices,
    AgentEventKind,
    AgentKind,
    AgentStatus,
)
from rekuest_core.enums import AssignPolicy
from .task import (
    TaskEventChoices,
    TaskEventKind,
    TaskInstructChoices,
    TaskInstructKind,
)
from .log import LogLevel, LogLevelChoices
from .signal import SignalKind, SignalKindChoices
from .state import JSONPatchOperation, RetentionPolicyChoices

__all__ = [
    # signal
    "SignalKind",
    "SignalKindChoices",
    # action
    "ActionKindChoices",
    "ActionScope",
    "DemandKind",
    "EffectsChoices",
    "ExecutionChoices",
    "HookKind",
    # agent
    "AgentEventChoices",
    "AgentEventKind",
    "AgentKind",
    "AgentStatus",
    # task
    "TaskEventChoices",
    "TaskEventKind",
    "TaskInstructChoices",
    "TaskInstructKind",
    "AssignPolicy",
    # log
    "LogLevel",
    "LogLevelChoices",
    # state
    "JSONPatchOperation",
    "RetentionPolicyChoices",
]
