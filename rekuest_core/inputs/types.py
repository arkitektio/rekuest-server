import dataclasses
from typing import Annotated, List, Optional, Protocol, runtime_checkable
from strawberry.experimental import pydantic
from strawberry import LazyType
from pydantic import BaseModel
from strawberry.types.unset import UnsetType
from rekuest_core.inputs import models
import strawberry
from rekuest_core import enums, scalars
from kante.unions import merged_input, union_member, union_member_types

# NOTE: For ``@strawberry.experimental.pydantic`` types, GraphQL field descriptions are
# sourced from the *pydantic* model (``Field(description=...)`` in ``models.py``), not from
# ``strawberry.field(description=...)``. The pydantic models are therefore the single source
# of truth for field documentation. Type-level descriptions (passed to the decorator or via a
# class docstring) still work here and are kept. Pure ``@strawberry.input`` types that are not
# backed by a pydantic model (``PortGroupInput``, ``DefinitionInput``) keep their field-level
# descriptions because those are read directly by strawberry.


@pydantic.input(
    models.EffectInputModel,
    description="""
    An effect is a way to modify a port based on a condition. For example,
    you could have an effect that hides the port if another port meets a condition,
    e.g. when the user selects a certain option in a dropdown, another port is hidden.

    The condition is a pure blok UtilCall (`call`) evaluated client-side against the
    catalog; it must return a boolean deciding whether the effect applies. `dependencies`
    is the authoritative list of other ports the call may reference (plus `value` for the
    port's own value).
    """,
)
class EffectInput:
    kind: enums.EffectKind
    dependencies: list[str] | None = strawberry.field(default_factory=list)
    call: Annotated["UtilCallInput", strawberry.lazy(__name__)]
    message: str | None = None
    fade: bool | None = True
    source: str | None = None


@pydantic.input(
    models.ChoiceInputModel,
    description="""
A choice is a value that can be selected in a dropdown.

It is composed of a value, a label, and a description. The value is the
value that is returned when the choice is selected. The label is the
text that is displayed in the dropdown. The description is the text
that is displayed when the user hovers over the choice.

    """,
)
class ChoiceInput:
    value: scalars.AnyDefault
    label: str
    image: str | None = None
    description: str | None = None


@pydantic.input(models.StateAccessorInputModel)
class StateAccessorInput:
    option_key: enums.OptionKey
    path: str | None = None
    call: Optional[Annotated["UtilCallInput", strawberry.lazy(__name__)]] = None


# ----------------------------------------------------------------------------
# Widgets arrive as kante discriminated input unions: one strict member input per kind, plus a
# merged wire type (`AssignWidgetInput` / `ReturnWidgetInput`) whose `to_pydantic` dispatches on
# `kind`. The members are published with `@unionElementOf` so generated clients rebuild the union.
# ----------------------------------------------------------------------------
@union_member("AssignWidgetInput", key="SLIDER")
@pydantic.input(models.SliderAssignWidgetInputModel, description="A numeric slider for INT, FLOAT and QUANTITY ports.")
class SliderAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None


@union_member("AssignWidgetInput", key="CHOICE")
@pydantic.input(models.ChoiceAssignWidgetInputModel, description="A dropdown over the port's `choices`.")
class ChoiceAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    placeholder: str | None = None


@union_member("AssignWidgetInput", key="STRING")
@pydantic.input(models.StringAssignWidgetInputModel, description="A text input for STRING ports.")
class StringAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    placeholder: str | None = None
    as_paragraph: bool | None = None


@union_member("AssignWidgetInput", key="SEARCH")
@pydantic.input(models.SearchAssignWidgetInputModel, description="A search over a ward for STRUCTURE ports (or lists of them).")
class SearchAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    query: scalars.SearchQuery
    ward: str
    filters: Optional[List[Annotated["ArgPortInput", strawberry.lazy(__name__)]]] = None
    dependencies: list[str] | None = strawberry.field(default_factory=list)
    placeholder: str | None = None


@union_member("AssignWidgetInput", key="CUSTOM")
@pydantic.input(models.CustomAssignWidgetInputModel, description="A catalog component rendered as the port's widget.")
class CustomAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    component: str
    props: Optional[List[Annotated["ComponentPropInput", strawberry.lazy(__name__)]]] = None
    dependencies: list[str] | None = strawberry.field(default_factory=list)
    fallback: Optional[Annotated["AssignWidgetInput", strawberry.lazy(__name__)]] = None


@union_member("AssignWidgetInput", key="STATE_CHOICE")
@pydantic.input(models.StateChoiceAssignWidgetInputModel, description="A choice over entries of an agent's state.")
class StateChoiceAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    dependency: str | None = None
    state_path: str | None = None
    state_call: Optional[Annotated["UtilCallInput", strawberry.lazy(__name__)]] = None
    state_accessors: Optional[List[Annotated["StateAccessorInput", strawberry.lazy(__name__)]]] = None
    dependencies: list[str] | None = strawberry.field(default_factory=list)


@union_member("AssignWidgetInput", key="PROXY")
@pydantic.input(models.ProxyAssignWidgetInputModel, description="Delegates the port to a port of another action.")
class ProxyAssignWidgetInput:
    kind: enums.AssignWidgetKind
    follow_value: str | None = None
    target_port: str
    target_action: str
    target_dependency: str | None = None


@merged_input(
    members=[SliderAssignWidgetInput, ChoiceAssignWidgetInput, StringAssignWidgetInput, SearchAssignWidgetInput, CustomAssignWidgetInput, StateChoiceAssignWidgetInput, ProxyAssignWidgetInput],
    noun="assign widget",
    description="An assign widget: the UI element used to assign a value to a port, as a discriminated union over `kind`. Only the fields of the chosen kind may be set; see the `*AssignWidgetInput` members.",
    descriptions={"kind": "Which kind of assign widget this is; decides which other fields are read."},
    spec=models.AssignWidgetInputModel,
)
class AssignWidgetInput:
    """An assign widget, discriminated by ``kind``."""


@union_member("ReturnWidgetInput", key="CHOICE")
@pydantic.input(models.ChoiceReturnWidgetInputModel, description="Displays the port's `choices` label for a returned value.")
class ChoiceReturnWidgetInput:
    kind: enums.ReturnWidgetKind


@union_member("ReturnWidgetInput", key="CUSTOM")
@pydantic.input(models.CustomReturnWidgetInputModel, description="A catalog component rendered for a returned value.")
class CustomReturnWidgetInput:
    kind: enums.ReturnWidgetKind
    component: str
    props: Optional[List[Annotated["ComponentPropInput", strawberry.lazy(__name__)]]] = None


@merged_input(
    members=[ChoiceReturnWidgetInput, CustomReturnWidgetInput],
    noun="return widget",
    description="A return widget: the UI element used to display a port's value, as a discriminated union over `kind`. Only the fields of the chosen kind may be set; see the `*ReturnWidgetInput` members.",
    descriptions={"kind": "Which kind of return widget this is; decides which other fields are read."},
    spec=models.ReturnWidgetInputModel,
)
class ReturnWidgetInput:
    """A return widget, discriminated by ``kind``."""


widget_input_types = [*union_member_types(AssignWidgetInput), *union_member_types(ReturnWidgetInput)]
"""The member input types; nothing references them, so every schema must list them in ``types=``."""


@pydantic.input(
    models.ValidatorInputModel,
    description="""
A validator for a port. `call` is a pure blok UtilCall evaluated client-side against the
catalog; it must return a boolean meaning 'valid'. Other ports the call references must be
listed in `dependencies` (the authoritative subscription list); `value` refers to the port's
own value. Use the .. syntax when traversing the tree of ports.
""",
)
class ValidatorInput:
    call: Annotated["UtilCallInput", strawberry.lazy(__name__)]
    dependencies: list[str] | None = strawberry.field(default_factory=list)
    label: str | None = None
    error_message: str | None = None
    source: str | None = None


@pydantic.input(
    models.OptimisticInputModel,
    description=""" An optimistic is used to optimistically set state values when the action is assigned. This is used to provide a better user experience by optimistically setting state values when the action is assigned, instead of waiting for the action to be executed and the state to be updated. This will only ever happen on the frontend.

""",
)
class OptimisticInput:
    state: str
    path: str | None = None
    path_call: Optional[Annotated["UtilCallInput", strawberry.lazy(__name__)]] = None
    accessor: str | None = None


@pydantic.input(models.RequiresInputModel)
class RequiresInput:
    key: str
    operator: enums.DescriptorOperator
    value: scalars.Arg | None = None


@pydantic.input(models.ProvidesInputModel)
class ProvidesInput:
    key: str
    operator: enums.DescriptorOperator
    value: scalars.Arg | None = None


@pydantic.input(
    models.ArgPortInputModel,
    description="""A Port is a single input or output of an action, identified by its `key` and typed by its `kind`.

    STRUCTURE, MEMORY_STRUCTURE and INTERFACE ports carry an `identifier` of the form `@package/key`
    (e.g. `@mikro/image`); ports with the same identifier are compatible. LIST and DICT ports have one
    child (the item type), UNION ports two or more (the variants), MODEL ports one per field. ENUM ports
    declare `choices`. See docs/design/ports.md for the full table.
    """,
)
class ArgPortInput:
    validators: list[ValidatorInput] | None = strawberry.field(default_factory=list)
    key: str
    label: str | None = None
    kind: enums.PortKind
    description: str | None = None
    identifier: str | None = None
    nullable: bool = False
    effects: list[EffectInput] | None = strawberry.field(default_factory=list)
    choices: list[ChoiceInput] | None = strawberry.field(default_factory=list)
    default: scalars.AnyDefault | None = None
    children: list[Annotated["ArgPortInput", strawberry.lazy(__name__)]] | None = strawberry.field(default_factory=list)
    widget: Optional[AssignWidgetInput] = None
    requires: list[RequiresInput] | None = strawberry.field(default_factory=list)
    reference_unit: str | None = None
    proposed_units: list[str] | None = None
    dimension: str | None = None


@pydantic.input(
    models.ReturnPortInputModel,
    description="""A Port is a single input or output of an action, identified by its `key` and typed by its `kind`.

    STRUCTURE, MEMORY_STRUCTURE and INTERFACE ports carry an `identifier` of the form `@package/key`
    (e.g. `@mikro/image`); ports with the same identifier are compatible. LIST and DICT ports have one
    child (the item type), UNION ports two or more (the variants), MODEL ports one per field. ENUM ports
    declare `choices`. See docs/design/ports.md for the full table.
    """,
)
class ReturnPortInput:
    key: str
    label: str | None = None
    kind: enums.PortKind
    description: str | None = None
    identifier: str | None = None
    nullable: bool = False
    effects: list[EffectInput] | None = strawberry.field(default_factory=list)
    choices: list[ChoiceInput] | None = strawberry.field(default_factory=list)
    children: list[Annotated["ReturnPortInput", strawberry.lazy(__name__)]] | None = strawberry.field(default_factory=list)
    widget: Optional[ReturnWidgetInput] = None
    provides: list[ProvidesInput] | None = strawberry.field(default_factory=list)
    reference_unit: str | None = None
    proposed_units: list[str] | None = None
    dimension: str | None = None


@strawberry.input(
    description="A Port Group is a group of ports that are related to each other. It is used to group ports together in the UI and provide a better user experience.",
)
class PortGroupInput:
    key: str = strawberry.field(description="The key of the port group. This is used to uniquely identify the port group")
    title: str | None = strawberry.field(default=None, description="The title of the port group, displayed in the UI")
    description: str | None = strawberry.field(default=None, description="The description of the port group, displayed in the UI")
    effects: list[EffectInput] | None = strawberry.field(default_factory=list, description="The effects applied to the port group as a whole")
    ports: list[str] = strawberry.field(default_factory=list, description="The keys of the root arg ports in this group; a port belongs to at most one group")


@pydantic.input(
    models.DescriptorInputModel,
    description="A single runtime descriptor key/value pair carried by a candidate object.",
)
class DescriptorInput:
    key: str
    value: scalars.Arg


@pydantic.input(
    models.PortMatchInputModel,
    description="""A structural (and optionally object-level) match against a port. Purely
    structural fields target the port shape; the optional ``descriptors`` carry a concrete
    runtime object's key/value pairs, evaluated against the port's compiled requires
    micro-constraint to find actions the object can actually be passed to.""",
)
class PortMatchInput:
    at: int | None = None
    key: str | None = None
    kind: enums.PortKind | None = None
    identifier: str | None = None
    nullable: bool | None = None
    dimension: str | None = None
    descriptors: list[DescriptorInput] | None = None
    children: Optional[list[Annotated["PortMatchInput", strawberry.lazy(__name__)]]] = None


@pydantic.input(
    models.ActionDemandInputModel,
    description="""Pure matching criteria for an action: hash or name short-circuits, arg/return
    port matches, protocols and forced port counts. Used directly by query filters and, wrapped
    in an ActionDependencyInput, by dependency declarations.""",
)
class ActionDemandInput:
    hash: str | None = None
    key: str | None = None
    app: str | None = None
    version: str | None = None
    name: str | None = None
    arg_matches: list[PortMatchInput] | None = None
    return_matches: list[PortMatchInput] | None = None
    protocols: list[str] | None = None
    force_arg_length: int | None = None
    force_return_length: int | None = None
    pure: bool | None = None
    idempotent: bool | None = None
    stateful: bool | None = None


@pydantic.input(
    models.StateDemandInputModel,
    description="""Pure matching criteria for a state definition: hash short-circuits, port
    matches and protocols. Used directly by query filters and, wrapped in a
    StateDependencyInput, by dependency declarations.""",
)
class StateDemandInput:
    hash: str | None = None
    key: str | None = None
    app: str | None = None
    matches: list[PortMatchInput] | None = None
    protocols: list[str] | None = None


@pydantic.input(
    models.ActionDependencyInputModel,
    description="""A named action requirement of a dependency: a slot key plus the demand the
    resolved action must satisfy, and resolution-lifecycle filters.""",
)
class ActionDependencyInput:
    key: str
    allow_inactive: bool | None = None
    description: str | None = None
    demand: ActionDemandInput | None = None
    optional: bool = False


@pydantic.input(
    models.StateDependencyInputModel,
    description="""A named state requirement of a dependency: a slot key plus the demand the
    agent's state must satisfy, and resolution-lifecycle filters.""",
)
class StateDependencyInput:
    key: str
    allow_inactive: bool | None = None
    description: str | None = None
    demand: StateDemandInput | None = None
    optional: bool = False


@pydantic.input(
    models.AgentDependencyInputModel,
    description="""A dependency for a implementation. By defining dependencies, you can
    create a dependency graph for your implementations and actions""",
)
class AgentDependencyInput:
    key: str
    app: str | None = None
    version: str | None = None
    auto_resolvable: bool = False
    name: str | None = None
    description: str | None = None
    optional: bool = False
    action_dependencies: list[ActionDependencyInput] | None = None
    state_dependencies: list[StateDependencyInput] | None = None
    min_viable_instances: int | None = None
    max_viable_instances: int | None = None
    mutually_exclusive_keys: list[str] | None = None
    prefered_instances: int | None = None


@pydantic.input(
    models.TestTargetInputModel,
    description="A test target: the action a test action tests, identified by exact hash or by an (app, key, version) coordinate. app defaults to the registering agent's app; omitting version matches every version.",
)
class TestTargetInput:
    hash: str | None = None
    app: str | None = None
    key: str | None = None
    version: str | None = None


@strawberry.input(
    description="""A definition

    Definitions are the building implementation for Actions and provide the
    information needed to create a action. They are primarly composed of a name,
    a description, and a list of ports.

    Definitions provide a protocol of input and output, and do not contain
    any information about the actual implementation of the action ( this is handled
    by a implementation that implements a action).

    """,
)
class DefinitionInput:
    description: str | None = strawberry.field(
        default=None,
        description="The description of the definition. This is the text that is displayed in the UI",
    )
    collections: list[str] = strawberry.field(
        default_factory=list,
        description="The collections of the definition. This is used to group definitions together in the UI",
    )
    key: str = strawberry.field(description="The key of the definition. This is used to uniquely identify the definition")
    version: str = strawberry.field(description="The version of the definition. This is used to differentiate if the underyling algorithm has changed, i.e we would expect different results for the same input")
    name: str = strawberry.field(description="The name of the actions. This is used to uniquely identify the definition")
    stateful: bool = strawberry.field(
        default=False,
        description="Whether the definition is stateful or not. If the definition is stateful, it can be used to create a stateful action. If the definition is not stateful, it cannot be used to create a stateful action",
    )
    pure: bool = strawberry.field(
        default=False,
        description="Whether the action is pure: same args always produce the same result and no side effects — its results are replayable/cacheable. Implies idempotent. Incompatible with stateful and with IRREVERSIBLE effects.",
    )
    idempotent: bool = strawberry.field(
        default=False,
        description="Whether the action is idempotent: safe to run multiple times with the same args without changing the outcome — on ambiguous executor loss it may be freely re-dispatched.",
    )
    allow_probe: bool = strawberry.field(
        default=False,
        description="Whether the action may be invoked as a probe: zero persistence, redis-held state, no history/replay/recovery. Only actions declaring this are callable via the call mutation.",
    )
    catalogs: list[str] | None = strawberry.field(
        default_factory=list,
        description="Names of the UI catalogs that extend the base catalog (`base@1`, always applied) for this definition's effect and validator calls. Unknown names yield an unknown_catalog warning; conflicting operation definitions across catalogs are a registration error.",
    )
    protocols: list[str] = strawberry.field(
        default_factory=list,
        description="Where the action means to hook in (`on_scene_annotation`): hints a caller filters on, stored beside the protocols the server infers. Not a contract, and not part of the action's hash.",
    )
    bloks: list[Annotated["ActionBlokInput", strawberry.lazy(__name__)]] = strawberry.field(
        default_factory=list,
        description="The action's own bloks, at most one per context: how a client shows a run of it (HTML in a task panel, OVERLAY drawn into the space that hosts the run). They name devices only by the action's dependency keys. Not part of the action's hash.",
    )
    port_groups: list[PortGroupInput] = strawberry.field(
        default_factory=list,
        description="The port groups of the definition. This is used to group ports together in the UI",
    )
    args: list[ArgPortInput] = strawberry.field(
        default_factory=list,
        description="The args of the definition. This is the input ports of the definition",
    )
    returns: list[ReturnPortInput] = strawberry.field(
        default_factory=list,
        description="The returns of the definition. This is the output ports of the definition",
    )
    kind: enums.ActionKind = strawberry.field(description="The kind of the definition. This is the type of the definition. Can be either a function or a generator")
    is_test_for: list[TestTargetInput] = strawberry.field(
        default_factory=list,
        description="The actions this definition is a test for, each identified by hash or by (app, key, version).",
    )
    is_dev: bool = strawberry.field(
        default=False,
        description="Whether the definition is a dev definition or not. If the definition is a dev definition, it can be used to create a dev action. If the definition is not a dev definition, it cannot be used to create a dev action",
    )


@pydantic.input(models.WindowInputModel, description="""A window that is calculated""")
class WindowInput:
    window_function: enums.WindowFunction
    label: str | None = None


@pydantic.input(models.TrackInputModel, description="""A value that is being tracked over time during the runtime of an action. This is the state of a dependency""")
class TrackInput:
    dependency_key: str | None = None
    state_key: str
    value_key: str
    label: str | None = None
    description: str | None = None
    windows: list[WindowInput] | None = None


@pydantic.input(
    models.ImplementationInputModel,
    description="""A implementation is a blueprint for a action. It is composed of a definition, a list of dependencies, and a list of params.""",
)
class ImplementationInput:
    definition: DefinitionInput
    interface: str
    instance_id: str | None = None
    params: scalars.AnyDefault | None = None
    locks: list[str] | None = None
    optimistics: list[OptimisticInput] | None = None
    tracks: list[TrackInput] | None = None
    manipulates: list[str] | None = None
    needs_token: bool = True
    provenance_audience: list[str] | None = None
    effects: enums.Effects = enums.Effects.UNKNOWN
    execution: enums.Execution = enums.Execution.PLAIN
    code_hash: str | None = None
    logo: str | None = None
    dependencies: list[AgentDependencyInput] = strawberry.field(default_factory=list)
    asks: Optional[List[Annotated["AskBlokInput", strawberry.lazy(__name__)]]] = None
    informs: Optional[List[Annotated["InformBlokInput", strawberry.lazy(__name__)]]] = None
    dynamic_asks: bool | None = None
    dynamic_calls: bool | None = None
    dynamic_informs: bool | None = None


@pydantic.input(
    models.StateDefinitionInputModel,
    description="""A state schema is a blueprint for a state. It is composed of a definition, a list of dependencies, and a list of params.""",
)
class StateDefinitionInput:
    name: str
    ports: list[ReturnPortInput] = strawberry.field(default_factory=list)


@pydantic.input(
    models.StateImplementationInputModel,
    description="""A state implementation is a blueprint for a state. It is composed of a definition, a list of dependencies, and a list of params.""",
)
class StateImplementationInput:
    interface: str
    definition: StateDefinitionInput
    key: str | None = None
    app: str | None = None


@pydantic.input(
    models.LockDefinitionInputModel,
    description="Which locks does the agent provide in general",
)
class LockDefinitionInput:
    key: str
    description: str | None = None


@pydantic.input(
    models.LockImplementationInputModel,
    description="Which locks does the agent provide in general",
)
class LockImplementationInput:
    key: str
    definition: LockDefinitionInput


@pydantic.input(models.AssetInputModel, description="A file an agent brings with it: the name its bloks call it by, and what the bytes are.")
class AssetInput:
    name: str
    kind: enums.AssetKind
    ref: str
    size: int | None = None
    source: str | None = None


@pydantic.input(models.DynamicValueInputModel, description="A bound state pointer referencing a variable inside a Blok state instance.")
class DynamicValueInput:
    literal: Optional[str] = None
    path: Optional[str] = None


@pydantic.input(models.AgentProbeInputModel, description="Defines a callback that routes user interactions directly to an Arkitekt Agent via Rekuest.")
class AgentProbeInput:
    dependency: str
    operation: str
    arguments: Optional[List[Annotated["ActionArgumentInput", strawberry.lazy(__name__)]]] = None


@pydantic.input(models.UtilCallInputModel, description="Defines a utility call that can be invoked within the system.")
class UtilCallInput:
    operation: str
    arguments: Optional[List[Annotated["ActionArgumentInput", strawberry.lazy(__name__)]]] = None


@pydantic.input(models.ActionArgumentInputModel, description="An argument of a call: a literal, a path, a nested call, a list or an object; null when it sets none.")
class ActionArgumentInput:
    key: str | None = None
    value_literal: Optional[scalars.JSONSerializable] = None
    value_path: Optional[str] = None

    agent_call: Optional[AgentProbeInput] = None
    util_call: Optional[UtilCallInput] = None
    value_list: Optional[List[Annotated["ActionArgumentInput", strawberry.lazy(__name__)]]] = None
    value_dict: Optional[List[Annotated["ActionArgumentInput", strawberry.lazy(__name__)]]] = None


@pydantic.input(models.ComponentPropInputModel, description="One prop of a component node: a literal, a path, a function call, a task call (named by `declaresValue`), an effect (`query.refetch`, `query.fetch_more`, `ui.copy`, `task.answer`), or nothing (null).")
class ComponentPropInput:
    key: str

    # Primitives mapping to standard properties, state paths, or actions
    static_value: Optional[scalars.JSONSerializable] = None
    dynamic_value: Optional[DynamicValueInput] = None
    agent_call: Optional[AgentProbeInput] = None
    util_call: Optional[UtilCallInput] = None
    declares_value: Optional[str] = None


@pydantic.input(models.ComponentNodeInputModel, description="A node of a blok tree (blok language v2).")
class ComponentNodeInput:
    id: str
    component: str
    props: Optional[List[ComponentPropInput]] = strawberry.field(default_factory=list)
    children: Optional[List[Annotated["ComponentNodeInput", strawberry.lazy(__name__)]]] = None


@runtime_checkable
class _Converts(Protocol):
    """A strawberry input made from a pydantic model."""

    def to_pydantic(self) -> BaseModel: ...


def _sent(value: object) -> object:
    """A sent input value as its pydantic model takes it: nested inputs converted."""
    if isinstance(value, list):
        return [_sent(item) for item in value]
    if isinstance(value, _Converts):
        return value.to_pydantic()
    return value


@strawberry.input(description="A local: blok-level form state that feeds a call, written only through `bind` on controls. A local is an arg port plus `persist`; its `kind` is a port kind or ANY. Every field left out (or null) is the tree's to tell: the kind is inferred (its default, the task argument it is passed to whole, the control bound to it; ANY when nothing tells), and an inferred port fills only the fields the local leaves out.")
class BlokLocalInput:
    key: str = strawberry.field(description="Its name, read bare in the tree (`{iterations}`); never a reserved root.")
    kind: enums.LocalKind | None = strawberry.field(default=strawberry.UNSET, description="A port kind or ANY. Left out or null: inferred from the tree, ANY when nothing tells.")
    label: str | None = strawberry.field(default=strawberry.UNSET, description="Its label.")
    description: str | None = strawberry.field(default=strawberry.UNSET, description="What it is for.")
    identifier: str | None = strawberry.field(default=strawberry.UNSET, description="The structure (or model, enum) identifier of what it holds.")
    nullable: bool | None = strawberry.field(default=strawberry.UNSET, description="Whether it may hold null; left out, what is inferred says (false when nothing does).")
    validators: list[ValidatorInput] | None = strawberry.field(default=strawberry.UNSET, description="Its validators.")
    effects: list[EffectInput] | None = strawberry.field(default=strawberry.UNSET, description="Its effects.")
    choices: list[ChoiceInput] | None = strawberry.field(default=strawberry.UNSET, description="The values it takes (an ENUM's).")
    default: scalars.AnyDefault | None = strawberry.field(default=strawberry.UNSET, description="The value it starts with.")
    children: list[ArgPortInput] | None = strawberry.field(default=strawberry.UNSET, description="Its child ports (the item of a LIST, the fields of a DICT or MODEL).")
    widget: Optional[AssignWidgetInput] = strawberry.field(default=strawberry.UNSET, description="The widget a form of it uses.")
    requires: list[RequiresInput] | None = strawberry.field(default=strawberry.UNSET, description="Its descriptor requirements.")
    reference_unit: str | None = strawberry.field(default=strawberry.UNSET, description="For a QUANTITY: its reference unit.")
    proposed_units: list[str] | None = strawberry.field(default=strawberry.UNSET, description="For a QUANTITY: the units offered.")
    dimension: str | None = strawberry.field(default=strawberry.UNSET, description="For a QUANTITY: its dimensionality (derived from the unit).")
    persist: bool | None = strawberry.field(default=strawberry.UNSET, description="Remember the last input on this device (never on a server); false when left out.")

    def to_pydantic(self) -> models.BlokLocalInputModel:
        """The local with only the fields that were sent: a field left out stays unset, so
        what the tree infers fills it (``model_fields_set``); a null is dropped by the model."""
        sent: dict[str, object] = {}
        for each in dataclasses.fields(self):
            value: object = getattr(self, each.name)
            if not isinstance(value, UnsetType):
                sent[each.name] = _sent(value)
        return models.BlokLocalInputModel.model_validate(sent)


@pydantic.input(models.BlokQueryInputModel, description="A declared read: one GraphQL query on a service, re-run when its variables change; the blok reads it as `queries.<key>`.")
class BlokQueryInput:
    key: str
    service: str
    document: str
    variables: list[ActionArgumentInput] = strawberry.field(default_factory=list)
    poll: Optional[float] = None
    more: Optional[str] = None


@pydantic.input(models.BlokSubscriptionInputModel, description="A declared stream: one GraphQL subscription on a service; the blok reads it as `subscriptions.<key>`.")
class BlokSubscriptionInput:
    key: str
    service: str
    document: str
    variables: list[ActionArgumentInput] = strawberry.field(default_factory=list)
    keep: Optional[int] = None
    history: Optional[str] = None


@pydantic.input(models.BlokDemoInputModel, description="What a preview renders a blok with, mirroring the roots it reads.")
class BlokDemoInput:
    deps: Optional[scalars.JSONSerializable] = None
    args: Optional[scalars.JSONSerializable] = None
    task: Optional[scalars.JSONSerializable] = None
    queries: Optional[scalars.JSONSerializable] = None
    subscriptions: Optional[scalars.JSONSerializable] = None


@pydantic.input(
    models.BlokImplementationInputModel,
    description="A blok an agent declares (blok language v2).",
)
class BlokImplementationInput:
    key: str
    vocab: int
    dependencies: list[AgentDependencyInput] = strawberry.field(default_factory=list)
    components: list[ComponentNodeInput] | None = None
    source: str | None = None
    module: Optional[str] = None
    description: Optional[str] = None
    context: enums.ComponentContext = enums.ComponentContext.HTML
    default: bool = False
    for_model: Optional[str] = None
    for_node: Optional[str] = None
    args: list[ArgPortInput] = strawberry.field(default_factory=list)
    catalogs: list[str] = strawberry.field(default_factory=list)
    locals: list[BlokLocalInput] = strawberry.field(default_factory=list)
    demo: Optional[BlokDemoInput] = None
    queries: list[BlokQueryInput] = strawberry.field(default_factory=list)
    subscriptions: list[BlokSubscriptionInput] = strawberry.field(default_factory=list)


@pydantic.input(models.AskBlokInputModel, description="A blok an action asks a person through, declared with it (blok language v2): a task blok, its args and returns ports.")
class AskBlokInput:
    key: str
    vocab: int
    description: Optional[str] = None
    components: list[ComponentNodeInput] | None = None
    source: str | None = None
    module: Optional[str] = None
    args: list[ArgPortInput] = strawberry.field(default_factory=list)
    returns: list[ReturnPortInput] = strawberry.field(default_factory=list)
    dependencies: list[AgentDependencyInput] = strawberry.field(default_factory=list)
    catalogs: list[str] = strawberry.field(default_factory=list)
    locals: list[BlokLocalInput] = strawberry.field(default_factory=list)
    demo: Optional[BlokDemoInput] = None
    queries: list[BlokQueryInput] = strawberry.field(default_factory=list)
    subscriptions: list[BlokSubscriptionInput] = strawberry.field(default_factory=list)


@pydantic.input(models.InformBlokInputModel, description="A blok an action shows a person while its task goes on, declared with it (blok language v2): a task blok, its args ports, no returns.")
class InformBlokInput:
    key: str
    vocab: int
    description: Optional[str] = None
    components: list[ComponentNodeInput] | None = None
    source: str | None = None
    module: Optional[str] = None
    args: list[ArgPortInput] = strawberry.field(default_factory=list)
    dependencies: list[AgentDependencyInput] = strawberry.field(default_factory=list)
    catalogs: list[str] = strawberry.field(default_factory=list)
    locals: list[BlokLocalInput] = strawberry.field(default_factory=list)
    demo: Optional[BlokDemoInput] = None
    queries: list[BlokQueryInput] = strawberry.field(default_factory=list)
    subscriptions: list[BlokSubscriptionInput] = strawberry.field(default_factory=list)


@pydantic.input(models.ActionBlokInputModel, description="An action's own blok (blok language v2): how a client shows a run of it in one context, naming devices by the action's dependency keys.")
class ActionBlokInput:
    vocab: int
    context: enums.ComponentContext = enums.ComponentContext.HTML
    components: list[ComponentNodeInput] | None = None
    source: str | None = None
    module: Optional[str] = None
    description: Optional[str] = None
    catalogs: list[str] = strawberry.field(default_factory=list)
    locals: list[BlokLocalInput] = strawberry.field(default_factory=list)
    demo: Optional[BlokDemoInput] = None
    queries: list[BlokQueryInput] = strawberry.field(default_factory=list)
    subscriptions: list[BlokSubscriptionInput] = strawberry.field(default_factory=list)


@pydantic.input(models.CatalogEventFieldInputModel, description="A field of the event a CALLBACK prop hands its callback (`event.<key>`).")
class CatalogEventFieldInput:
    key: str
    kind: enums.CatalogValueKind
    description: Optional[str] = None


@pydantic.input(models.CatalogPropInputModel, description="A prop a catalog component accepts.")
class CatalogPropInput:
    key: str
    kind: enums.CatalogValueKind
    required: bool = False
    description: Optional[str] = None
    event: Optional[List[CatalogEventFieldInput]] = None


@pydantic.input(models.CatalogComponentInputModel, description="A component a UI catalog can render.")
class CatalogComponentInput:
    name: str
    description: Optional[str] = None
    props: List[CatalogPropInput] = strawberry.field(default_factory=list)
    accepts_children: bool = True
    context: enums.ComponentContext = enums.ComponentContext.HTML
    children_context: Optional[enums.ComponentContext] = None
    inherits_context: bool = False
    downstream_of: List[str] = strawberry.field(default_factory=list)
    not_downstream_of: List[str] = strawberry.field(default_factory=list)
    text_prop: Optional[str] = None
    catalog: Optional[str] = None


@pydantic.input(models.CatalogArgumentInputModel, description="An argument a catalog operation accepts.")
class CatalogArgumentInput:
    key: str
    kind: enums.CatalogValueKind
    required: bool = True
    description: Optional[str] = None


@pydantic.input(models.CatalogOperationInputModel, description="A pure operation a UI catalog can evaluate for UtilCalls.")
class CatalogOperationInput:
    name: str
    description: Optional[str] = None
    arguments: List[CatalogArgumentInput] = strawberry.field(default_factory=list)
    returns: enums.CatalogValueKind


@pydantic.input(models.WidgetDefaultInputModel, description="A catalog's default widget for ports matching a kind and/or structure identifier. A UI applies it when a port has no explicit widget; an identifier match beats a kind match.")
class WidgetDefaultInput:
    kind: Optional[enums.PortKind] = None
    identifier: Optional[str] = None
    widget: Optional[AssignWidgetInput] = None
    return_widget: Optional[ReturnWidgetInput] = None
