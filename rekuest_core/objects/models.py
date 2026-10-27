import strawberry
from typing import List, Optional
from pydantic import BaseModel, JsonValue, field_validator
from typing import Literal, Union
import datetime
from rekuest_core import enums
from typing import Any


class ChoiceModel(BaseModel):
    label: str
    value: Any
    image: str | None = None
    description: str | None = None


class AssignWidgetModel(BaseModel):
    kind: str
    follow_value: str | None = None


class SliderAssignWidgetModel(AssignWidgetModel):
    kind: Literal["SLIDER"]
    min: float | None = None
    max: float | None = None
    step: float | None = None


class ChoiceAssignWidgetModel(AssignWidgetModel):
    """A dropdown over the port's own `choices`."""

    kind: Literal["CHOICE"]
    placeholder: str | None = None


class CustomAssignWidgetModel(AssignWidgetModel):
    """A catalog component rendered as the port's widget. The port value is in scope as the reserved root `value`."""

    kind: Literal["CUSTOM"]
    component: str
    props: Optional[List["ComponentPropModel"]] = None
    dependencies: list[str] | None = None
    fallback: Optional["AssignWidgetModelUnion"] = None


class SearchAssignWidgetModel(AssignWidgetModel):
    kind: Literal["SEARCH"]
    query: str  # TODO: Validators
    ward: str
    filters: list["ArgPortModel"] | None = None
    dependencies: list[str] | None = None
    placeholder: str | None = None


class StateAccessorModel(BaseModel):
    option_key: enums.OptionKey
    path: str | None = None
    call: Optional["UtilCallModel"] = None


# `StateChoiceAssignWidgetModel` used to be declared twice here, byte for byte. The
# second definition shadowed the first, so only one was ever reachable.
class StateChoiceAssignWidgetModel(AssignWidgetModel):
    kind: Literal["STATE_CHOICE"]
    state_path: str | None = None
    state_call: Optional["UtilCallModel"] = None
    dependency: str | None = None
    state_accessors: list[StateAccessorModel] | None = None
    dependencies: list[str] | None = None


class ProxyWidgetModel(AssignWidgetModel):
    kind: Literal["PROXY"]
    target_port: str
    target_action: str
    target_dependency: str | None = None


class StringWidgetModel(AssignWidgetModel):
    kind: Literal["STRING"]
    placeholder: str | None
    as_paragraph: bool | None


AssignWidgetModelUnion = Union[
    SliderAssignWidgetModel,
    ChoiceAssignWidgetModel,
    SearchAssignWidgetModel,
    StateChoiceAssignWidgetModel,
    StringWidgetModel,
    CustomAssignWidgetModel,
    ProxyWidgetModel,
]


class ReturnWidgetModel(BaseModel):
    kind: str


class CustomReturnWidgetModel(ReturnWidgetModel):
    """A catalog component rendered for a returned value. The value is in scope as the reserved root `value`."""

    kind: Literal["CUSTOM"]
    component: str
    props: Optional[List["ComponentPropModel"]] = None


class ChoiceReturnWidgetModel(ReturnWidgetModel):
    """Displays the label of the port's own `choices` for a returned value."""

    kind: Literal["CHOICE"]


ReturnWidgetModelUnion = Union[CustomReturnWidgetModel, ChoiceReturnWidgetModel]


class EffectModel(BaseModel):
    kind: str
    call: "UtilCallModel"
    dependencies: list[str]
    source: str | None = None


class MessageEffectModel(EffectModel):
    kind: Literal["MESSAGE"]
    message: str


class HideEffectModel(EffectModel):
    kind: Literal["HIDE"]
    fade: bool = True


class CustomEffectModel(EffectModel):
    kind: Literal["CUSTOM"]


EffectModelUnion = Union[MessageEffectModel, HideEffectModel, CustomEffectModel]


class PortGroupModel(BaseModel):
    key: str
    title: str | None
    description: str | None
    effects: list[EffectModelUnion] | None = None
    ports: list[str]


class ValidatorModel(BaseModel):
    call: "UtilCallModel"
    dependencies: list[str] | None = []
    label: str | None = None
    error_message: str | None = None
    source: str | None = None


class PortMatchModel(BaseModel):
    at: int | None = None
    key: str | None = None
    kind: str | None = None
    identifier: str | None = None
    children: list["PortMatchModel"] | None = None
    nullable: bool | None = None
    dimension: str | None = None


class RequiresModel(BaseModel):
    key: str
    operator: enums.DescriptorOperator
    value: Any = None


class ProvidesModel(BaseModel):
    key: str
    operator: enums.DescriptorOperator
    value: Any = None


class OptimisticModel(BaseModel):
    state: str
    path: str | None = None
    path_call: Optional["UtilCallModel"] = None
    accessor: str | None = None


class PortModel(BaseModel):
    key: str
    label: str | None = None
    kind: enums.PortKind
    description: str | None = None
    identifier: str | None = None
    nullable: bool = False
    effects: list[EffectModelUnion] | None = None
    children: list["PortModel"] | None = None
    choices: list[ChoiceModel] | None = None
    reference_unit: str | None = None
    proposed_units: list[str] | None = None
    dimension: str | None = None


class ArgPortModel(PortModel):
    validators: list[ValidatorModel] | None = None
    default: Any | None = None
    children: list["ArgPortModel"] | None = None
    widget: Optional[AssignWidgetModelUnion] = None
    requires: list[RequiresModel] | None = None


class ReturnPortModel(PortModel):
    children: list["ReturnPortModel"] | None = None
    widget: Optional[ReturnWidgetModelUnion] = None
    provides: list[ProvidesModel] | None = None


class WindowModel(BaseModel):
    window_function: enums.WindowFunction
    label: str | None = None


class TrackModel(BaseModel):
    dependency_key: str | None = None
    state_key: str
    value_key: str
    label: str | None = None
    description: str | None = None
    windows: list[WindowModel] | None = None


class DefinitionModel(BaseModel):
    id: strawberry.ID
    hash: str
    name: str
    kind: enums.ActionKind
    description: str | None
    port_groups: list[PortGroupModel]
    collections: list[str]
    scope: enums.ActionScope
    is_test_for: list[str]
    tests: list[str]
    protocols: list[str]
    defined_at: datetime.datetime
    is_dev: bool = False
    args: list[ArgPortModel]
    returns: list[ReturnPortModel]
    optimistics: list[OptimisticModel] | None = None


class DynamicValueModel(BaseModel):
    """Base model for a dynamic value input, which can reference a variable in a Blok state instance.

    Attributes:
        literal: An optional static fallback literal value, passed as a serialized string or JSON primitive.
    """

    literal: str | None = None
    path: str | None = None


class AgentCallModel(BaseModel):
    """Base model for defining a callback that routes user interactions directly to an Arkitekt Agent via Rekuest.

    Attributes:
        target_dependency_key: The abstract agent dependency key declared in the Blok manifest (e.g., 'stage_dep').
        operation_name: The target function name registered on that specific agent's worker thread loop.
        arguments: An optional list of key-value arguments compiled for the target agent call.
    """

    dependency: str
    operation: str
    arguments: Optional[List["ActionArgumentModel"]] = None


class UtilCallModel(BaseModel):
    operation: str
    arguments: Optional[List["ActionArgumentModel"]] = None


class ActionArgumentModel(BaseModel):
    """Base model for an action argument input, which can be a static literal or a dynamic state reference.

    Attributes:
        key: The argument property name.
        value_literal: An optional static literal string value if not dynamically bound.
        value_path: An optional JSON Pointer referencing the shared Blok state to inject into this argument slot dynamically.
    """

    key: str | None = None
    value_literal: JsonValue = None
    value_path: Optional[str] = None

    # Separated nested calls
    agent_call: Optional["AgentCallModel"] = None
    util_call: Optional["UtilCallModel"] = None

    value_list: Optional[List["ActionArgumentModel"]] = None
    value_dict: Optional[List["ActionArgumentModel"]] = None


# ============================================================================
# 2. Abstract Component Property Bindings
# ============================================================================
class ComponentPropModel(BaseModel):
    """Base model for a single key-value prop configuration for a component layout node.

    Attributes:
        key: The prop key name matching the target UI catalog constraint.
        static_value: An optional raw scalar or JSON-stringified literal configuration parameter (e.g., '40x' or True).
        dynamic_value: An optional reactive state data-binding rule.
        agent_action: An optional imperative interactive network action callback loop.
    """

    key: str
    static_value: JsonValue = None
    dynamic_value: Optional[DynamicValueModel] = None
    declares_value: Optional[str] = None  # The record a task call writes (``into``), or a foreach's loop variable.

    # Separated top-level callbacks
    agent_call: Optional[AgentCallModel] = None
    util_call: Optional[UtilCallModel] = None


class ComponentNodeModel(BaseModel):
    id: str
    component: str
    props: Optional[List[ComponentPropModel]] = None
    children: Optional[List["ComponentNodeModel"]] = None


class WidgetDefaultModel(BaseModel):
    """A catalog's default widget for ports matching a kind and/or structure identifier."""

    kind: str | None = None
    identifier: str | None = None
    widget: Optional[AssignWidgetModelUnion] = None
    return_widget: Optional[ReturnWidgetModelUnion] = None


SearchAssignWidgetModel.model_rebuild()
CustomAssignWidgetModel.model_rebuild()
CustomReturnWidgetModel.model_rebuild()
StateAccessorModel.model_rebuild()
StateChoiceAssignWidgetModel.model_rebuild()
OptimisticModel.model_rebuild()


# ============================================================================
# UI catalog registry
# ============================================================================
class CatalogEventFieldModel(BaseModel):
    key: str
    kind: enums.CatalogValueKind
    description: str | None = None


class CatalogPropModel(BaseModel):
    key: str
    kind: enums.CatalogValueKind
    required: bool = False
    description: str | None = None
    event: list[CatalogEventFieldModel] | None = None


class CatalogComponentModel(BaseModel):
    name: str
    description: str | None = None
    props: list[CatalogPropModel] = []
    accepts_children: bool = True
    context: enums.ComponentContext = enums.ComponentContext.HTML
    children_context: enums.ComponentContext | None = None
    inherits_context: bool = False
    downstream_of: list[str] = []
    not_downstream_of: list[str] = []
    text_prop: str | None = None
    catalog: str | None = None


class CatalogArgumentModel(BaseModel):
    key: str
    kind: enums.CatalogValueKind
    required: bool = True
    description: str | None = None


class CatalogOperationModel(BaseModel):
    name: str
    description: str | None = None
    arguments: list[CatalogArgumentModel] = []
    returns: enums.CatalogValueKind


class BlokLocalModel(BaseModel):
    """A stored local as written: an arg port's fields, its kind a
    :class:`~rekuest_core.enums.LocalKind` (a port kind or ANY) or None when it was left out,
    plus ``persist``."""

    key: str
    label: str | None = None
    kind: enums.LocalKind | None = None
    description: str | None = None
    identifier: str | None = None
    nullable: bool = False
    effects: list[EffectModelUnion] | None = None
    children: list[ArgPortModel] | None = None
    choices: list[ChoiceModel] | None = None
    reference_unit: str | None = None
    proposed_units: list[str] | None = None
    dimension: str | None = None
    validators: list[ValidatorModel] | None = None
    default: JsonValue = None
    widget: Optional[AssignWidgetModelUnion] = None
    requires: list[RequiresModel] | None = None
    persist: bool = False


class ResolvedBlokLocalModel(BlokLocalModel):
    """A stored local as the blok's tree resolves it: its kind always set."""

    kind: enums.LocalKind


class BlokQueryModel(BaseModel):
    key: str
    service: str
    document: str
    variables: list[ActionArgumentModel] = []
    poll: float | None = None
    more: str | None = None


class BlokSubscriptionModel(BaseModel):
    key: str
    service: str
    document: str
    variables: list[ActionArgumentModel] = []
    keep: int | None = None
    history: str | None = None


class BlokDemoModel(BaseModel):
    deps: JsonValue = None
    args: JsonValue = None
    task: JsonValue = None
    queries: JsonValue = None
    subscriptions: JsonValue = None


class DiagnosticModel(BaseModel):
    """A finding on a registered row: a warning of its last write, or what the blok v2 migration's
    converter found (stored as the vocabulary's ``Finding.to_json()``, level in lowercase)."""

    level: enums.DiagnosticLevel = enums.DiagnosticLevel.WARNING
    code: str
    message: str
    path: str | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _upper_level(cls, level: object) -> object:
        """``error``/``warning`` (the vocabulary's spelling) read as ERROR/WARNING."""
        return level.upper() if isinstance(level, str) else level
