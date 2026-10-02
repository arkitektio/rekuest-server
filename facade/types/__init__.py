"""GraphQL object types for the facade app, split into domain submodules.

This ``__init__`` re-exports every public type so the established
``from facade import types`` / ``types.X`` access keeps working.

Because the types densely cross-reference each other (e.g. ``Agent`` references
``Task``), the submodules use
``from __future__ import annotations`` so no annotation is evaluated at import
time (avoiding import cycles). At the bottom of this module every public type is
injected into each submodule's globals, so that the string forward references
resolve against the defining module's namespace when Strawberry builds the schema.
"""

from . import (
    action,
    agent,
    schedule,
    trigger,
    task,
    auth,
    base,
    blok,
    probe,
    dashboard,
    demand,
    dependency,
    implementation,
    session,
    shelve,
    state,
    structure,
    testcase,
    threed,
    toolbox,
)
from .action import Action, ActionStats, ActionStatsResolver
from .agent import Agent, Lock
from .schedule import Schedule
from .trigger import Signal, SignalDeclaration, Trigger
from .task import (
    Task,
    TaskEvent,
    TaskInstruct,
    TaskStats,
    TaskStatsResolver,
)
from .probe import Probe, ProbeEvent, ProbeStats
from .auth import App, Caller, Client, Device, Organization, Release, User
from .base import build_prescoped_queryset, build_prescoper
from .blok import Blok, BlokAgentMapping, BlokDependency, MaterializedBlok
from .dashboard import BaseCatalog, Dashboard, DashboardPlacement, UICatalog
from .demand import (
    ActionDemand,
    ActionDemandModel,
    ActionDependency,
    ActionDependencyModel,
    StateDemand,
    StateDemandModel,
    StateDependency,
    StateDependencyModel,
)
from .dependency import (
    AgentMapping,
    Dependency,
    DependencyTree,
    ImplementationMapping,
    ResolvedAgentDependency,
    ResolvedDependency,
    Resolution,
)
from .implementation import Implementation
from .session import Session, SessionBoundary, TaskBoundary
from .shelve import MemoryDrawer, MemoryShelve
from .state import (
    JSONPatch,
    Patch,
    Snapshot,
    State,
    StateDefinition,
)
from .structure import (
    Interface,
    PortUsage,
    Structure,
    StructurePackage,
)
from .testcase import TestCase, TestResult
from .threed import Placement, Space, ThreeDModel
from .toolbox import Collection, Protocol, Shortcut, Toolbox

__all__ = [
    "build_prescoped_queryset",
    "build_prescoper",
    "Action",
    "ActionStats",
    "ActionStatsResolver",
    "ActionDemand",
    "ActionDependency",
    "ActionDemandModel",
    "StateDemand",
    "StateDependency",
    "StateDemandModel",
    "User",
    "Device",
    "App",
    "Release",
    "Client",
    "Organization",
    "Caller",
    "Schedule",
    "Signal",
    "SignalDeclaration",
    "Trigger",
    "Collection",
    "Protocol",
    "Toolbox",
    "Shortcut",
    "Dependency",
    "ResolvedDependency",
    "Resolution",
    "ImplementationMapping",
    "AgentMapping",
    "DependencyTree",
    "ResolvedAgentDependency",
    "Implementation",
    "Lock",
    "Agent",
    "MemoryShelve",
    "MemoryDrawer",
    "Task",
    "Probe",
    "ProbeEvent",
    "ProbeStats",
    "TaskStats",
    "TaskStatsResolver",
    "TaskEvent",
    "TaskInstruct",
    "TestCase",
    "TestResult",
    "Dashboard",
    "UICatalog",
    "BaseCatalog",
    "DashboardPlacement",
    "Blok",
    "BlokDependency",
    "MaterializedBlok",
    "BlokAgentMapping",
    "StateDefinition",
    "State",
    "JSONPatch",
    "Patch",
    "Snapshot",
    "StructurePackage",
    "Interface",
    "Structure",
    "PortUsage",
    "TaskBoundary",
    "SessionBoundary",
    "Session",
    "ThreeDModel",
    "Space",
    "Placement",
]

# --- Forward-reference resolution -------------------------------------------------
# Inject every public type into each submodule's namespace so that the string
# forward references in their annotations resolve when Strawberry builds the schema.
_SUBMODULES = (
    action,
    agent,
    schedule,
    trigger,
    task,
    auth,
    blok,
    probe,
    dashboard,
    demand,
    dependency,
    implementation,
    session,
    shelve,
    state,
    structure,
    testcase,
    threed,
    toolbox,
)
_EXPORTED = {name: globals()[name] for name in __all__}
for _submodule in _SUBMODULES:
    for _name, _obj in _EXPORTED.items():
        setattr(_submodule, _name, _obj)
