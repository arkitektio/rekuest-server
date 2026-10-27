import dataclasses
import datetime
from typing import Annotated, Any, List, Optional
import strawberry
import strawberry_django
from pydantic import BaseModel
from strawberry import LazyType
from strawberry.experimental import pydantic

from rekuest_core.objects import models
from rekuest_core import enums, scalars


class ChoiceModel(BaseModel):
    label: str
    value: str
    image: str | None
    description: str | None


@pydantic.type(models.ChoiceModel)
class Choice:
    label: str
    value: scalars.AnyDefault = strawberry.field(description="The value the choice stands for (any JSON value fitting the port's kind).")
    image: str | None = None
    description: str | None = None


@pydantic.interface(models.AssignWidgetModel)
class AssignWidget:
    kind: enums.AssignWidgetKind
    follow_value: str | None


@pydantic.type(models.ProxyWidgetModel)
class ProxyWidget(AssignWidget):
    target_port: str = strawberry.field(description="The port key of the port that we are going to target with a proxy widget. This is used for proxy widgets")
    target_action: str = strawberry.field(description="The action dependency name that we are going to target with a proxy widget. This is used for proxy widgets")
    target_dependency: str | None = strawberry.field(description="The dependency that we are going to target with a proxy widget. This is used for proxy widgets")


@pydantic.type(models.SliderAssignWidgetModel)
class SliderAssignWidget(AssignWidget):
    min: float | None
    max: float | None
    step: float | None


@pydantic.type(models.ChoiceAssignWidgetModel, description="A dropdown over the port's own `choices`.")
class ChoiceAssignWidget(AssignWidget):
    placeholder: str | None = None


@pydantic.type(models.CustomAssignWidgetModel, description="A catalog component rendered as the port's widget.")
class CustomAssignWidget(AssignWidget):
    component: str = strawberry.field(description="The catalog component to render. The port value is in scope as the reserved root `value`: read it via a prop bound to `/value`, write it back via a prop that declares the value `value`.")
    props: Optional[List[Annotated["ComponentProp", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Props of the component. value_paths may only reference `value` and the widget's `dependencies`; agent calls are not allowed.")
    dependencies: list[str] | None = strawberry.field(default=None, description="The other ports (port paths) whose values the props may reference.")
    fallback: Optional[Annotated["AssignWidget", strawberry.lazy(__name__)]] = strawberry.field(default=None, description="Widget to render when the UI has no such component in its catalog.")


@pydantic.type(models.StateAccessorModel)
class StateAccessor:
    option_key: enums.OptionKey
    path: str | None = strawberry.field(default=None, description="Static JSON pointer into the state value ('/x/y'). Omitted: the whole value.")
    call: Optional[Annotated["UtilCall", strawberry.lazy(__name__)]] = strawberry.field(default=None, description="Pure UtilCall returning the pointer dynamically; may reference `state`, `value` and the widget's `dependencies`.")


@pydantic.type(models.StateChoiceAssignWidgetModel)
class StateChoiceAssignWidget(AssignWidget):
    state_path: str | None = strawberry.field(default=None, description="Static JSON pointer into the state value that provides the choices.")
    state_call: Optional[Annotated["UtilCall", strawberry.lazy(__name__)]] = strawberry.field(default=None, description="Pure UtilCall returning the pointer dynamically; may reference `state`, `value` and `dependencies`.")
    dependency: str | None
    state_accessors: list[StateAccessor] | None
    dependencies: list[str] | None = strawberry.field(default=None, description="The other ports (port paths) whose values the calls may reference.")


@pydantic.type(models.StringWidgetModel)
class StringAssignWidget(AssignWidget):
    placeholder: str | None = None
    as_paragraph: bool | None = None


@pydantic.interface(models.ReturnWidgetModel)
class ReturnWidget:
    kind: enums.ReturnWidgetKind


@pydantic.type(models.CustomReturnWidgetModel, description="A catalog component rendered for a returned value.")
class CustomReturnWidget(ReturnWidget):
    component: str = strawberry.field(description="The catalog component to render. The returned value is in scope as the reserved root `value`.")
    props: Optional[List[Annotated["ComponentProp", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Props of the component; value_paths may only reference `value`.")


@pydantic.type(models.ChoiceReturnWidgetModel, description="Displays the label of the port's own `choices` for a returned value.")
class ChoiceReturnWidget(ReturnWidget):
    kind: enums.ReturnWidgetKind


def _call_as_json(call: Any) -> Any:
    """A call tree as plain JSON, whether strawberry hands us the pydantic model or its dataclass twin."""
    if hasattr(call, "model_dump"):
        return call.model_dump(mode="json", exclude_none=True)
    if dataclasses.is_dataclass(call):
        return {k: v for k, v in dataclasses.asdict(call).items() if v is not None}
    return call


@pydantic.interface(models.EffectModel)
class Effect:
    kind: enums.EffectKind
    call: Annotated["UtilCall", strawberry.lazy(__name__)] = strawberry.field(
        description="The pure blok UtilCall evaluated client-side against the catalog. It must return a boolean: for effects whether the effect applies, for validators whether the value is valid. Argument value_paths may only reference names listed in `dependencies`, plus `value` for the port's own value."
    )
    dependencies: list[str]

    source: Optional[str] = strawberry.field(default=None, description="The authoring expression the call was compiled from, if the client provided one. Informational: never parsed or validated by the server.")

    @strawberry_django.field(description="The full call tree as raw JSON, so deep trees are not truncated by fragment depth.")
    def call_json(self) -> scalars.JSONSerializable:
        return _call_as_json(self.call)


@pydantic.type(models.MessageEffectModel)
class MessageEffect(Effect):
    message: str


@pydantic.type(models.CustomEffectModel, description="An effect whose behaviour is entirely defined by its call.")
class CustomEffect(Effect):
    kind: enums.EffectKind


@pydantic.type(models.HideEffectModel)
class HideEffect(Effect):
    fade: bool = True


@pydantic.type(models.PortMatchModel)
class PortMatch:
    at: int | None = strawberry.field(
        default=None,
        description="The index of the port to match. ",
    )
    key: str | None = strawberry.field(
        default=None,
        description="The key of the port to match.",
    )
    kind: enums.PortKind | None = strawberry.field(
        default=None,
        description="The kind of the port to match. ",
    )
    identifier: str | None = strawberry.field(
        default=None,
        description="The identifier of the port to match. ",
    )
    nullable: bool | None = strawberry.field(
        default=None,
        description="Whether the port is nullable. ",
    )
    dimension: str | None = strawberry.field(
        default=None,
        description="The canonical pint dimensionality the port must have (QUANTITY wiring-compatibility key).",
    )
    children: list[Annotated["PortMatch", strawberry.lazy(__name__)]] | None = strawberry.field(
        default=None,
        description="Child ports to match. ",
    )


@pydantic.type(models.PortGroupModel)
class PortGroup:
    key: str
    title: str | None
    description: str | None
    effects: list[Effect] | None
    ports: list[str]


@pydantic.type(models.ValidatorModel)
class Validator:
    call: Annotated["UtilCall", strawberry.lazy(__name__)] = strawberry.field(
        description="The pure blok UtilCall evaluated client-side against the catalog. It must return a boolean: for effects whether the effect applies, for validators whether the value is valid. Argument value_paths may only reference names listed in `dependencies`, plus `value` for the port's own value."
    )
    dependencies: list[str] | None
    label: str | None
    error_message: str | None = None

    source: Optional[str] = strawberry.field(default=None, description="The authoring expression the call was compiled from, if the client provided one. Informational: never parsed or validated by the server.")

    @strawberry_django.field(description="The full call tree as raw JSON, so deep trees are not truncated by fragment depth.")
    def call_json(self) -> scalars.JSONSerializable:
        return _call_as_json(self.call)


@pydantic.type(models.RequiresModel)
class Requires:
    key: str = strawberry.field(description="The key of the descriptor. This is used to uniquely identify the descriptor")
    value: scalars.Arg | None = strawberry.field(default=None, description="The value the constraint compares against (none for EXISTS).")
    operator: enums.DescriptorOperator = strawberry.field(description="How the constraint compares the object's value at `key` with `value`.")


@pydantic.type(models.ProvidesModel)
class Provides:
    key: str = strawberry.field(description="The key of the descriptor. This is used to uniquely identify the descriptor")
    value: scalars.Arg | None = strawberry.field(default=None, description="The value the constraint compares against (none for EXISTS).")
    operator: enums.DescriptorOperator = strawberry.field(description="How the constraint compares the object's value at `key` with `value`.")


@pydantic.type(models.WindowModel, description="""A window that is calculated""")
class Window:
    window_function: enums.WindowFunction
    label: str | None = None


@pydantic.type(models.TrackModel, description="""A value that is being tracked over time during the runtime of an action. This is the state of a dependency""")
class Track:
    dependency_key: str | None = None
    state_key: str
    value_key: str
    label: str | None = None
    description: str | None = None
    windows: list[Window] | None = None


@pydantic.type(models.ArgPortModel)
class ArgPort:
    identifier: scalars.Identifier | None = strawberry.field(
        default=None,
        description="The identifier of the port. Identifier are used to give meaning to structure ports",
    )
    default: scalars.AnyDefault | None
    kind: enums.PortKind
    key: str
    nullable: bool
    label: str | None
    description: str | None
    effects: list[Effect] | None = None
    children: list[Annotated["ArgPort", strawberry.lazy(__name__)]] | None = None
    choices: list[Choice] | None = None
    widget: AssignWidget | None = None
    validators: list[Validator] | None = None
    requires: list[Requires] | None = None
    reference_unit: str | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: the canonical/reference unit, e.g. 'volt'. Default selection; other units of the same dimension are still allowed.",
    )
    proposed_units: list[str] | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: units offered as a dropdown in the UI (proposals only; any unit of the same dimension is still valid).",
    )
    dimension: str | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: the pint dimensionality string. This is the wiring-compatibility key between quantity ports.",
    )


@pydantic.type(models.ReturnPortModel)
class ReturnPort:
    identifier: scalars.Identifier | None = strawberry.field(
        default=None,
        description="The identifier of the port. Identifier are used to give meaning to structure ports",
    )
    kind: enums.PortKind
    key: str
    nullable: bool
    label: str | None
    description: str | None
    effects: list[Effect] | None = None
    children: list[Annotated["ReturnPort", strawberry.lazy(__name__)]] | None = None
    choices: list[Choice] | None = None
    widget: ReturnWidget | None = None
    provides: list[Provides] | None = None
    reference_unit: str | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: the canonical/reference unit, e.g. 'volt'. Default selection; other units of the same dimension are still allowed.",
    )
    proposed_units: list[str] | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: units offered as a dropdown in the UI (proposals only; any unit of the same dimension is still valid).",
    )
    dimension: str | None = strawberry.field(
        default=None,
        description="For QUANTITY ports: the pint dimensionality string. This is the wiring-compatibility key between quantity ports.",
    )


@pydantic.type(models.SearchAssignWidgetModel)
class SearchAssignWidget(AssignWidget):
    query: str
    ward: str
    filters: list[ArgPort] | None = None
    dependencies: list[str] | None = None
    placeholder: str | None = None


# TODO: Should be saved and made accessible
@pydantic.type(models.OptimisticModel)
class Optimistic:
    """An optimistic is used to optimistically set state values when the action is assigned. This is used to provide a better user experience by optimistically setting state values when the action is assigned, instead of waiting for the action to be executed and the state to be updated. This will only ever happen on the frontend."""

    state: str
    path: str | None = strawberry.field(default=None, description="Static JSON pointer into the state value to set.")
    path_call: Optional[Annotated["UtilCall", strawberry.lazy(__name__)]] = strawberry.field(default=None, description="Pure UtilCall returning the pointer dynamically; may reference `args` (the assignment arguments).")
    accessor: str | None = strawberry.field(default=None, description="Static JSON pointer into the assignment args for the value to set; omitted: the whole args.")


@pydantic.type(models.DefinitionModel)
class Definition:
    hash: scalars.ActionHash
    name: str
    stateful: bool
    kind: enums.ActionKind
    description: str | None
    port_groups: list[PortGroup]
    collections: list[str]
    scope: enums.ActionScope
    is_test_for: list[str]
    tests: list[str]
    protocols: list[str]
    defined_at: datetime.datetime
    is_dev: bool

    @strawberry_django.field()
    def args(self) -> list[ArgPort]:
        return [models.ArgPortModel(**i) for i in self.args]

    @strawberry_django.field()
    def returns(self) -> list[ReturnPort]:
        return [models.ReturnPortModel(**i) for i in self.returns]


@pydantic.type(models.DynamicValueModel, description="A bound state pointer referencing a variable inside a Blok state instance.")
class DynamicValue:
    literal: Optional[str] = strawberry.field(default=None, description="A static fallback literal value (passed as a serialized string/JSON primitive).")
    path: Optional[str] = strawberry.field(default=None, description="JSON Pointer to a variable inside the Blok's isolated data model (e.g., '/microscope/exposure').")


@pydantic.type(models.AgentCallModel, description="Defines a callback that routes user interactions directly to an Arkitekt Agent via Rekuest.")
class AgentCall:
    dependency: str = strawberry.field(description="The abstract agent dependency key declared in the Blok manifest (e.g., 'stage_dep').")
    operation: str = strawberry.field(description="The target function name registered on that specific agent's worker thread loop.")
    arguments: Optional[List[Annotated["ActionArgument", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Key-value arguments map compiled for the target agent call.")


@pydantic.type(models.UtilCallModel, description="Defines a utility call that can be invoked within the system.")
class UtilCall:
    operation: str = strawberry.field(description="The utility function name to invoke.")
    arguments: Optional[List[Annotated["ActionArgument", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Key-value arguments map compiled for the target utility call.")


@pydantic.type(models.ActionArgumentModel, description="A JSON-serializable argument entry for a multi-agent action trigger.")
class ActionArgument:
    key: str | None = strawberry.field(default=None, description="The argument property name.")
    value_literal: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="Static literal string value if not dynamically bound.")
    value_path: Optional[str] = strawberry.field(default=None, description="JSON Pointer referencing the shared Blok state to inject into this argument slot dynamically.")

    agent_call: Optional[AgentCall] = strawberry.field(default=None, description="Defines a nested agent call if this argument should trigger an agent interaction.")
    util_call: Optional[UtilCall] = strawberry.field(default=None, description="Defines a nested utility call if this argument should trigger a system utility interaction.")
    value_list: Optional[List[Annotated["ActionArgument", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Defines a list of values if this argument should be an array.")
    value_dict: Optional[List[Annotated["ActionArgument", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="Defines a list of key-value pairs if this argument should be a dictionary.")


@pydantic.type(models.ComponentPropModel, description="One prop of a component node: a literal, a path, a function call, a task call (named by `declaresValue`), an effect, or nothing (null).")
class ComponentProp:
    key: str = strawberry.field(description="The prop key name matching the target UI catalog constraint.")

    # Primitives mapping to standard properties, state paths, or actions
    static_value: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="A JSON literal: a string, number, bool, list or object.")
    dynamic_value: Optional[DynamicValue] = strawberry.field(default=None, description="A path the prop reads.")
    agent_call: Optional[AgentCall] = strawberry.field(default=None, description="Defines an imperative interactive network action callback loop if this prop should trigger an agent interaction.")
    util_call: Optional[UtilCall] = strawberry.field(default=None, description="A function the prop's value is, or in a callback the effect it runs (ui.copy, task.answer, query.refetch, query.fetch_more).")
    declares_value: Optional[str] = strawberry.field(default=None, description="The name a task call writes its record to (`... into move`), or a foreach's loop variable (its `let`).")


@pydantic.type(models.ComponentNodeModel, description="An abstract structural visual element inside a Blok blueprint manifest.")
class ComponentNode:
    id: str
    component: str
    props: Optional[List[ComponentProp]] = strawberry.field(description="Properties or configuration for the component.")
    children: Optional[List[Annotated["ComponentNode", strawberry.lazy(__name__)]]] = strawberry.field(description="List of child component node IDs.")


@pydantic.type(models.CatalogPropModel, description="A prop a catalog component accepts.")
class CatalogProp:
    key: str = strawberry.field(description="The prop key a ComponentProp.key must match.")
    kind: enums.CatalogValueKind = strawberry.field(description="The value kind this prop accepts.")
    required: bool = strawberry.field(description="Whether every component instance must set this prop.")
    description: Optional[str] = strawberry.field(default=None, description="Human-readable description of the prop.")
    event: Optional[List[Annotated["CatalogEventField", strawberry.lazy(__name__)]]] = strawberry.field(default=None, description="For a CALLBACK prop: the fields of the event it hands its callback (`event.<key>`).")


@pydantic.type(models.CatalogEventFieldModel, description="A field of the event a CALLBACK prop hands its callback.")
class CatalogEventField:
    key: str = strawberry.field(description="The field, read as `event.<key>`.")
    kind: enums.CatalogValueKind = strawberry.field(description="Its value kind.")
    description: Optional[str] = strawberry.field(default=None, description="Human-readable description of the field.")


@pydantic.type(models.CatalogComponentModel, description="A component a UI catalog can render.")
class CatalogComponent:
    name: str = strawberry.field(description="The component name a ComponentNode.component must match.")
    description: Optional[str] = strawberry.field(default=None, description="Human-readable description of the component.")
    props: List[CatalogProp] = strawberry.field(description="The props this component accepts.")
    accepts_children: bool = strawberry.field(description="Whether children may be nested under this component.")
    context: enums.ComponentContext = strawberry.field(description="The context this component may be placed in.")
    children_context: Optional[enums.ComponentContext] = strawberry.field(default=None, description="The context in force for this component's children, when it differs from its own.")
    inherits_context: bool = strawberry.field(default=False, description="Whether the component may be placed in any context, taking its parent's for itself and its children.")
    downstream_of: List[str] = strawberry.field(description="Component names of which at least one must be an ancestor, at any depth.")
    not_downstream_of: List[str] = strawberry.field(description="Component names of which none may be an ancestor.")
    text_prop: Optional[str] = strawberry.field(default=None, description="The prop text children fill (`<Button>Go</Button>` sets `label`); null: it takes no text.")
    catalog: Optional[str] = strawberry.field(default=None, description="The catalog it belongs to, as name@version (`ui@1`), or the name of the module it is from.")


@pydantic.type(models.BlokLocalModel, description="A local as the blok declares it: blok-level form state that feeds a call, written only through `bind` on controls. A local is an arg port plus `persist`; its kind may be left out.")
class BlokLocal:
    key: str = strawberry.field(description="Its name, read bare in the tree.")
    kind: enums.LocalKind | None = strawberry.field(default=None, description="What it holds, as a port's kind (what the controls bound to it write and what a task call may pass it to), or ANY: an untyped value. Null: the blok left it out, and its tree tells it (see `resolvedLocals`).")
    identifier: scalars.Identifier | None = strawberry.field(default=None, description="The structure (or model, enum) identifier of what it holds.")
    nullable: bool = strawberry.field(default=False, description="Whether it may hold null.")
    label: str | None = strawberry.field(default=None, description="Its label.")
    description: str | None = strawberry.field(default=None, description="What it is for.")
    default: scalars.AnyDefault | None = strawberry.field(default=None, description="The value it starts with.")
    children: list[ArgPort] | None = strawberry.field(default=None, description="Its child ports (the item of a LIST, the fields of a DICT or MODEL).")
    choices: list[Choice] | None = strawberry.field(default=None, description="The values it takes (an ENUM's).")
    validators: list[Validator] | None = strawberry.field(default=None, description="Its validators.")
    effects: list[Effect] | None = strawberry.field(default=None, description="Its effects.")
    widget: AssignWidget | None = strawberry.field(default=None, description="The widget a form of it uses.")
    requires: list[Requires] | None = strawberry.field(default=None, description="Its descriptor requirements.")
    reference_unit: str | None = strawberry.field(default=None, description="For a QUANTITY: its reference unit.")
    proposed_units: list[str] | None = strawberry.field(default=None, description="For a QUANTITY: the units offered.")
    dimension: str | None = strawberry.field(default=None, description="For a QUANTITY: its dimensionality.")
    persist: bool = strawberry.field(default=False, description="Whether a renderer remembers the last input on the device (never on a server).")


@pydantic.type(models.ResolvedBlokLocalModel, description="A local as the blok's tree resolves it: its kind always set (inferred from its default, the task argument it is passed to or the control bound to it when the blok left it out; ANY when nothing tells).")
class ResolvedBlokLocal:
    key: str = strawberry.field(description="Its name, read bare in the tree.")
    kind: enums.LocalKind = strawberry.field(description="What it holds, as a port's kind (what the controls bound to it write and what a task call may pass it to), or ANY: an untyped value. Always set: a kind the blok left out is inferred from its tree.")
    identifier: scalars.Identifier | None = strawberry.field(default=None, description="The structure (or model, enum) identifier of what it holds.")
    nullable: bool = strawberry.field(default=False, description="Whether it may hold null.")
    label: str | None = strawberry.field(default=None, description="Its label.")
    description: str | None = strawberry.field(default=None, description="What it is for.")
    default: scalars.AnyDefault | None = strawberry.field(default=None, description="The value it starts with.")
    children: list[ArgPort] | None = strawberry.field(default=None, description="Its child ports (the item of a LIST, the fields of a DICT or MODEL).")
    choices: list[Choice] | None = strawberry.field(default=None, description="The values it takes (an ENUM's).")
    validators: list[Validator] | None = strawberry.field(default=None, description="Its validators.")
    effects: list[Effect] | None = strawberry.field(default=None, description="Its effects.")
    widget: AssignWidget | None = strawberry.field(default=None, description="The widget a form of it uses.")
    requires: list[Requires] | None = strawberry.field(default=None, description="Its descriptor requirements.")
    reference_unit: str | None = strawberry.field(default=None, description="For a QUANTITY: its reference unit.")
    proposed_units: list[str] | None = strawberry.field(default=None, description="For a QUANTITY: the units offered.")
    dimension: str | None = strawberry.field(default=None, description="For a QUANTITY: its dimensionality.")
    persist: bool = strawberry.field(default=False, description="Whether a renderer remembers the last input on the device (never on a server).")


@pydantic.type(models.BlokQueryModel, description="A declared read: one GraphQL query on a service, read as `queries.<key>` (status, value, error, refetching, fetching_more).")
class BlokQuery:
    key: str = strawberry.field(description="Its name.")
    service: str = strawberry.field(description="The service it runs on, by its key.")
    document: str = strawberry.field(description="The GraphQL query.")
    variables: List[ActionArgument] = strawberry.field(description="Its variables, each an expression over args, locals and dependencies.")
    poll: Optional[float] = strawberry.field(default=None, description="Re-run every this many seconds, besides when its variables change.")
    more: Optional[str] = strawberry.field(default=None, description="The list field of its result that `queries.<key>.fetch_more(...)` appends new pages to, as a dotted path into the data; null: it cannot fetch more.")


@pydantic.type(models.BlokSubscriptionModel, description="A declared stream: one GraphQL subscription on a service, read as `subscriptions.<key>`.")
class BlokSubscription:
    key: str = strawberry.field(description="Its name.")
    service: str = strawberry.field(description="The service it runs on, by its key.")
    document: str = strawberry.field(description="The GraphQL subscription.")
    variables: List[ActionArgument] = strawberry.field(description="Its variables.")
    keep: Optional[int] = strawberry.field(default=None, description="The last this many payloads are kept as a list; the latest only when null.")
    history: Optional[str] = strawberry.field(default=None, description="The declared query it replays from while travelling.")


@pydantic.type(models.BlokDemoModel, description="What a preview renders a blok with, mirroring the roots it reads.")
class BlokDemo:
    deps: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="Per dependency key (`self` included), its states and tasks.")
    args: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="What it is opened on.")
    task: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="The task record it is about.")
    queries: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="Per declared query, the value it answers.")
    subscriptions: Optional[scalars.JSONSerializable] = strawberry.field(default=None, description="Per declared subscription, its value.")


@pydantic.type(models.CatalogArgumentModel, description="An argument a catalog operation accepts.")
class CatalogArgument:
    key: str = strawberry.field(description="The argument key a UtilCall argument must use.")
    kind: enums.CatalogValueKind = strawberry.field(description="The value kind of the argument.")
    required: bool = strawberry.field(description="Whether every call must pass this argument.")
    description: Optional[str] = strawberry.field(default=None, description="Human-readable description of the argument.")


@pydantic.type(models.CatalogOperationModel, description="A pure operation a UI catalog can evaluate for UtilCalls.")
class CatalogOperation:
    name: str = strawberry.field(description="The operation name a UtilCall.operation must match.")
    description: Optional[str] = strawberry.field(default=None, description="Human-readable description of the operation.")
    arguments: List[CatalogArgument] = strawberry.field(description="The arguments the operation accepts.")
    returns: enums.CatalogValueKind = strawberry.field(description="The kind of value the operation returns.")


@pydantic.type(models.DiagnosticModel, description="A non-fatal registration finding, e.g. a validator call naming an operation neither the base catalog nor the named catalog provides.")
class Diagnostic:
    level: enums.DiagnosticLevel = strawberry.field(description="Severity. An ERROR is stored only by the blok v2 migration, for what its converter could not carry over; writes store WARNINGs.")
    code: str = strawberry.field(description="Machine-readable code: a blok's are the blok vocabulary's (`catalog.props`, `palette.raw_colour`, `upgrade.multi_step`), a definition's unknown_operation, unknown_catalog.")
    message: str = strawberry.field(description="Human-readable explanation.")
    path: Optional[str] = strawberry.field(default=None, description="Where the finding applies (owner label).")


@pydantic.type(models.WidgetDefaultModel, description="A catalog's default widget for ports matching a kind and/or structure identifier. A UI applies it when a port has no explicit widget; an identifier match beats a kind match.")
class WidgetDefault:
    kind: Optional[enums.PortKind] = strawberry.field(default=None, description="Port kind the default applies to (with `identifier`: both must match).")
    identifier: Optional[str] = strawberry.field(default=None, description="Structure identifier the default applies to, e.g. '@mikro/image'.")
    widget: Optional[AssignWidget] = strawberry.field(default=None, description="The assign widget to render for matching argument ports.")
    return_widget: Optional[ReturnWidget] = strawberry.field(default=None, description="The return widget to render for matching return ports.")
