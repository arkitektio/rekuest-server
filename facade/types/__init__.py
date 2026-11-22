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
    auth,
    blok,
    dashboard,
    demand,
    dependency,
    implementation,
    probe,
    schedule,
    session,
    shelve,
    state,
    structure,
    task,
    testcase,
    threed,
    toolbox,
    trigger,
    wiregram,
)
from .action import Action
from .agent import Agent, Lock
from .asset import (
    AgentAsset,
    Asset,
    AssetPolicy,
    AssetUploadGrant,
    ImagePolicy,
    ModelPolicy,
    ModelStats,
)
from .auth import App, Caller, Client, Device, Organization, Release, User
from .base import build_prescoped_queryset
from .blok import (
    ActionBlok,
    Blok,
    BlokAgentMapping,
    BlokDependency,
    BlokEdit,
    BlokRevision,
    MaterializedBlok,
    RevisionDependency,
)
from .dashboard import BaseCatalog, Dashboard, DashboardPlacement, Module
from .demand import (
    ActionDemand,
    ActionDemandModel,
    ActionDependency,
    StateDemand,
    StateDemandModel,
    StateDependency,
)
from .dependency import (
    AgentMapping,
    Dependency,
    DependencyTree,
    ImplementationMapping,
    Resolution,
    ResolvedAgentDependency,
    ResolvedDependency,
)
from .implementation import Implementation, ImplementationInteraction
from .probe import Probe, ProbeEvent, ProbeStats, TaskInquiry
from .schedule import Schedule
from .session import Session, SessionBoundary, TaskBoundary
from .shelve import MemoryDrawer, MemoryShelve
from .state import (
    JSONPatch,
    Patch,
    Snapshot,
    State,
    StateDefinition,
)
from .stats import (
    ActionStats,
    AgentStats,
    TaskStats,
    action_stats,
    agent_stats,
    task_stats,
)
from .structure import (
    HostedStructure,
    Interface,
    PortUsage,
    Service,
    Structure,
    StructureDescriptor,
    StructurePackage,
)
from .task import (
    ActionAllowance,
    Task,
    TaskEvent,
    TaskInstruct,
)
from .testcase import TestCase, TestResult
from .threed import Placement, Space
from .toolbox import Collection, Protocol, Shortcut, ShortcutBinding, Toolbox
from .trigger import Firing, Signal, SignalDeclaration, Trigger
from .wiregram import Wiregram

__all__ = [
    "build_prescoped_queryset",
    "Action",
    "ActionStats",
    "action_stats",
    "AgentStats",
    "agent_stats",
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
    "Firing",
    "Wiregram",
    "SignalDeclaration",
    "Trigger",
    "Collection",
    "Protocol",
    "Toolbox",
    "Shortcut",
    "ShortcutBinding",
    "Dependency",
    "ResolvedDependency",
    "Resolution",
    "ImplementationMapping",
    "AgentMapping",
    "DependencyTree",
    "ResolvedAgentDependency",
    "Implementation",
    "ImplementationInteraction",
    "Lock",
    "Agent",
    "MemoryShelve",
    "MemoryDrawer",
    "Task",
    "Probe",
    "ProbeEvent",
    "ProbeStats",
    "TaskInquiry",
    "TaskStats",
    "task_stats",
    "TaskEvent",
    "TaskInstruct",
    "ActionAllowance",
    "TestCase",
    "TestResult",
    "Dashboard",
    "Module",
    "BaseCatalog",
    "DashboardPlacement",
    "Blok",
    "BlokDependency",
    "MaterializedBlok",
    "BlokAgentMapping",
    "BlokEdit",
    "BlokRevision",
    "RevisionDependency",
    "StateDefinition",
    "State",
    "JSONPatch",
    "Patch",
    "Snapshot",
    "StructurePackage",
    "Interface",
    "Service",
    "Structure",
    "StructureDescriptor",
    "HostedStructure",
    "PortUsage",
    "TaskBoundary",
    "SessionBoundary",
    "Session",
    "Asset",
    "AgentAsset",
    "AssetPolicy",
    "AssetUploadGrant",
    "ImagePolicy",
    "ModelPolicy",
    "ModelStats",
    "Space",
    "ActionBlok",
    "Placement",
]

# --- Forward-reference resolution -------------------------------------------------
# Inject every public type into each submodule's namespace so that the string
# forward references in their annotations resolve when Strawberry builds the schema.
_SUBMODULES = (
    action,
    agent,
    schedule,
    wiregram,
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
