"""Django ORM models for the facade app, split into domain submodules.

This ``__init__`` imports every model so Django's app registry discovers them, and
re-exports them so both ``from facade import models`` / ``models.X`` and the
``from facade.models import X`` direct-symbol imports keep working. Cross-submodule
foreign keys use string references (``"Action"``) which Django resolves via the app
registry, so submodule import order does not matter.
"""

from .action import (
    Action,
    ActionLivenessDirty,
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
from .signal import Firing, Signal, SignalDeclaration, Trigger
from .wiregram import Wiregram
from .structure import Descriptor, Service, StructureDeclaration
from .task import (
    Task,
    TaskEvent,
    TaskInstruct,
)
from .blok import (
    ActionBlok,
    Blok,
    BlokAgentMapping,
    BlokArgPort,
    BlokRevision,
    BlokDependency,
    Dashboard,
    DashboardPlacement,
    MaterializedBlok,
)
from .catalog import (
    Collection,
    Protocol,
    Shortcut,
    ShortcutBinding,
    Toolbox,
    Module,
)
from .implementation import (
    Dependency,
    Implementation,
    ImplementationInteraction,
    InteractionRole,
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
from .asset import AgentAsset, Asset
from .threed import Placement, Space, SpacePlacement

__all__ = [
    # caller
    "Caller",
    # schedule
    "Schedule",
    # signals
    "Signal",
    "Descriptor",
    "Firing",
    "Wiregram",
    "SignalDeclaration",
    "Trigger",
    # structures
    "Service",
    "StructureDeclaration",
    # catalog
    "Collection",
    "Protocol",
    "Module",
    "Toolbox",
    "Shortcut",
    "ShortcutBinding",
    # action
    "Action",
    "ActionLivenessDirty",
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
    "ImplementationInteraction",
    "InteractionRole",
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
    "ActionBlok",
    "Blok",
    "BlokDependency",
    "MaterializedBlok",
    "DashboardPlacement",
    "BlokAgentMapping",
    "BlokArgPort",
    "BlokRevision",
    # threed
    "SpacePlacement",
    "Asset",
    "AgentAsset",
    "Space",
    "Placement",
    # signals (connected on import)
    "signals",
]

import facade.signals as signals  # noqa: E402,F401
