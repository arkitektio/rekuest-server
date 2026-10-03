"""Strawberry-django filters and orders for the facade app, split by domain.

This ``__init__`` re-exports every public filter/order so the established
``from facade import filters`` / ``filters.X`` access keeps working. Submodules use
``from __future__ import annotations`` and cross-referenced filters are injected into
each submodule's namespace below, so nested filter references resolve at schema-build time.
"""

from . import (
    action,
    agent,
    automation,
    task,
    auth,
    blok,
    common,
    dashboard,
    dependency,
    implementation,
    session,
    shelve,
    testcase,
    threed,
    toolbox,
)
from .action import ActionFilter, ActionOrder
from .automation import ScheduleFilter, ScheduleOrder, SignalFilter, SignalOrder, TriggerFilter, TriggerOrder
from .agent import (
    AgentFilter,
    AgentOrder,
    ImplementationAgentFilter,
)
from .task import (
    TaskEventFilter,
    TaskEventOrder,
    TaskFilter,
    TaskInstructFilter,
    TaskOrder,
)
from .auth import (
    ClientFilter,
    ClientOrder,
    OrganizationFilter,
    OrganizationOrder,
    UserFilter,
    UserOrder,
)
from .blok import MaterializedBlokFilter, MaterializedBlokOrder
from .common import ParamPair
from .dashboard import DashboardPlacementFilter, DashboardPlacementOrder
from .dependency import (
    BlokDependencyFilter,
    DependencyFilter,
    ResolutionFilter,
    ResolvedDependencyFilter,
)
from .implementation import (
    ImplementationActionFilter,
    ImplementationFilter,
    ImplementationOrder,
)
from .session import SessionFilter, SessionOrder
from .shelve import (
    MemoryDrawerFilter,
    MemoryShelveFilter,
    MemoryShelveOrder,
)
from .testcase import TestCaseFilter, TestResultFilter
from .threed import (
    PlacementFilter,
    PlacementOrder,
    SpaceFilter,
    SpaceOrder,
    ThreeDModelFilter,
    ThreeDModelOrder,
)
from .toolbox import (
    ProtocolFilter,
    ProtocolOrder,
    ShortcutFilter,
    ShortcutOrder,
    ToolboxFilter,
    ToolboxOrder,
)

__all__ = [
    "ParamPair",
    "UserFilter",
    "UserOrder",
    "OrganizationOrder",
    "OrganizationFilter",
    "ClientOrder",
    "ClientFilter",
    "AgentFilter",
    "AgentOrder",
    "ImplementationAgentFilter",
    "MemoryShelveFilter",
    "MemoryShelveOrder",
    "MemoryDrawerFilter",
    "TaskOrder",
    "TaskFilter",
    "TaskEventOrder",
    "TaskEventFilter",
    "TaskInstructFilter",
    "TestCaseFilter",
    "TestResultFilter",
    "ResolutionFilter",
    "ResolvedDependencyFilter",
    "DependencyFilter",
    "BlokDependencyFilter",
    "ProtocolOrder",
    "ShortcutOrder",
    "ToolboxOrder",
    "ProtocolFilter",
    "ToolboxFilter",
    "ShortcutFilter",
    "ActionOrder",
    "ActionFilter",
    "ImplementationOrder",
    "ImplementationActionFilter",
    "ImplementationFilter",
    "ThreeDModelOrder",
    "ThreeDModelFilter",
    "SpaceOrder",
    "SpaceFilter",
    "PlacementOrder",
    "PlacementFilter",
    "MaterializedBlokOrder",
    "MaterializedBlokFilter",
    "DashboardPlacementFilter",
    "DashboardPlacementOrder",
    "SessionOrder",
    "SessionFilter",
    "ScheduleFilter",
    "ScheduleOrder",
    "TriggerFilter",
    "TriggerOrder",
    "SignalFilter",
    "SignalOrder",
]

# --- Nested-filter reference resolution -------------------------------------------
# Inject every public filter into each submodule so the string forward references in
# nested filter annotations resolve when Strawberry builds the schema.
_SUBMODULES = (
    action,
    agent,
    automation,
    task,
    auth,
    blok,
    common,
    dashboard,
    dependency,
    implementation,
    session,
    shelve,
    testcase,
    threed,
    toolbox,
)
_EXPORTED = {name: globals()[name] for name in __all__}
for _submodule in _SUBMODULES:
    for _name, _obj in _EXPORTED.items():
        setattr(_submodule, _name, _obj)
