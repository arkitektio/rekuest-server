"""Django ORM models for the facade app, split into domain submodules.

This ``__init__`` imports every model so Django's app registry discovers them, and
re-exports them so both ``from facade import models`` / ``models.X`` and the
``from facade.models import X`` direct-symbol imports keep working. Cross-submodule
foreign keys use string references (``"Action"``) which Django resolves via the app
registry, so submodule import order does not matter.
"""

from .action import (
    Action,
    ArgPort,
    BasePort,
    ReturnPort,
)
from .agent import (
    Agent,
    Lock,
    MemoryDrawer,
    MemoryShelve,
)
from .schedule import Schedule
from .signal import Signal, SignalDeclaration, Trigger
from .task import (
    Task,
    TaskEvent,
    TaskInstruct,
)
from .blok import (
    Blok,
    BlokAgentMapping,
    BlokDependency,
    Dashboard,
    DashboardPlacement,
    MaterializedBlok,
)
from .catalog import (
    Collection,
    Protocol,
    Shortcut,
    Toolbox,
    UICatalog,
)
from .implementation import (
    Dependency,
    Implementation,
    Resolution,
    ResolvedDependency,
)
from .caller import Caller
from .state import (
    Patch,
    Session,
    Snapshot,
    State,
    StateDefinition,
)
from .testcase import TestCase, TestResult
from .threed import Placement, Space, ThreeDModel

__all__ = [
    # caller
    "Caller",
    # schedule
    "Schedule",
    # signals
    "Signal",
    "SignalDeclaration",
    "Trigger",
    # catalog
    "Collection",
    "Protocol",
    "UICatalog",
    "Toolbox",
    "Shortcut",
    # action
    "Action",
    "BasePort",
    "ArgPort",
    "ReturnPort",
    # agent
    "Lock",
    "Agent",
    "MemoryShelve",
    "MemoryDrawer",
    # implementation
    "Dependency",
    "Resolution",
    "ResolvedDependency",
    "Implementation",
    # task
    "Task",
    "TaskEvent",
    "TaskInstruct",
    # testcase
    "TestCase",
    "TestResult",
    # state
    "StateDefinition",
    "State",
    "Session",
    "Patch",
    "Snapshot",
    # blok
    "Dashboard",
    "Blok",
    "BlokDependency",
    "MaterializedBlok",
    "DashboardPlacement",
    "BlokAgentMapping",
    # threed
    "ThreeDModel",
    "Space",
    "Placement",
    # signals (connected on import)
    "signals",
]

import facade.signals as signals  # noqa: E402,F401
