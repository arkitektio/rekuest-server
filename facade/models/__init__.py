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
    Command,
    CommandCursor,
    Lock,
    MemoryDrawer,
    MemoryShelve,
)
from .asset import AgentAsset, Asset
from .blok import (
    ActionBlok,
    Blok,
    BlokAgentMapping,
    BlokArgPort,
    BlokDependency,
    BlokRevision,
    Dashboard,
    DashboardPlacement,
    MaterializedBlok,
)
from .caller import Caller
from .catalog import (
    Collection,
    Module,
    Protocol,
    Shortcut,
    ShortcutBinding,
    Toolbox,
)
from .implementation import (
    Dependency,
    Implementation,
    ImplementationFit,
    ImplementationInteraction,
    InteractionRole,
    Resolution,
    ResolvedDependency,
)
from .schedule import Schedule
from .signal import Firing, ObjectDescriptors, Signal, SignalDeclaration, Trigger
from .state import (
    Patch,
    Session,
    Snapshot,
    State,
    StateDefinition,
)
from .structure import Descriptor, Service, StructureDeclaration
from .task import (
    ActionAllowance,
    Task,
    TaskEvent,
    TaskInstruct,
)
from .testcase import TestCase, TestResult
from .threed import Placement, Space
from .wiregram import Wiregram

__all__ = [
    # caller
    "Caller",
    # schedule
    "Schedule",
    # signals
    "ObjectDescriptors",
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
    "Command",
    "CommandCursor",
    "MemoryShelve",
    "MemoryDrawer",
    # implementation
    "Dependency",
    "Resolution",
    "ResolvedDependency",
    "Implementation",
    "ImplementationFit",
    "ImplementationInteraction",
    "InteractionRole",
    # task
    "Task",
    "TaskEvent",
    "TaskInstruct",
    "ActionAllowance",
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
    "Asset",
    "AgentAsset",
    "Space",
    "Placement",
    # signals (connected on import)
    "signals",
]

import facade.signals as signals  # noqa: E402,F401
