import hashlib
import json
import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Iterator, List, Literal, Optional, Union
from rekuest_core import enums, units
from rekuest_core.values import value_mismatch
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SerializerFunctionWrapHandler, field_validator, model_serializer, model_validator
from typing_extensions import Self
from strawberry.types.unset import UnsetType

if TYPE_CHECKING:
    from rekuest_core.vocabulary.vocabulary import Finding


class BindsInputModel(BaseModel):
    implementations: Optional[list[str]] = Field(description="The implementations (by id) that are allowed to fulfill this bind. If None, any implementation is allowed.")
    clients: Optional[list[str]] = Field(description="The clients (by id) that are allowed to fulfill this bind. If None, any client is allowed.")
    desired_instances: int = Field(default=1, description="The desired number of instances that should fulfill this bind.")
    minimum_instances: int = Field(default=1, description="The minimum number of instances that must fulfill this bind for it to be viable.")


# Path grammar shared by port dependencies and call arguments
# ----------------------------------------------------------------------------
# * A **port path** is a ``..``-separated sequence of port keys walking ``children``:
#   ``foo``, ``foo..bar``, ``foo..bar..baz``.
# * A ``dependencies`` entry is a port path.
# * A **value_path** is ``[/]<root>[/<json-pointer-into-the-value>...]``. Its first ``/``
#   segment (the root) is compared verbatim against the allowed roots -- ``dependencies``
#   plus ``value`` for ports, demo-state keys / declared values / dependency keys for bloks.
#   Everything after the first ``/`` is a JSON pointer into that value and is not validated
#   server-side.
# * ``value`` is reserved for the port's own value; a port may not be keyed ``value``.
PORT_PATH_SEPARATOR = ".."

#: The conventional key of a LIST or DICT item port; the only key that may contain the separator.
ITEM_KEY = "..."
_KEY_RESERVED = {"value"}


def _check_port_key(key: str, owner: str) -> None:
    """A key is non-empty, not reserved, and free of the port-path separator (except the item key)."""
    if not key:
        raise ValueError(f"{owner}: port key must not be empty")
    if key in _KEY_RESERVED:
        raise ValueError(f"{owner}: {key!r} is a reserved port key (it names the port's own value in calls)")
    if PORT_PATH_SEPARATOR in key and key != ITEM_KEY:
        raise ValueError(f"{owner}: port key {key!r} may not contain {PORT_PATH_SEPARATOR!r}")


def _check_unique(items: list, attr: str, owner: str) -> None:
    seen: set = set()
    for item in items:
        value = getattr(item, attr)
        if value in seen:
            raise ValueError(f"{owner}: duplicate {attr} {value!r}")
        seen.add(value)



def _value_path_root(value_path: str) -> str:
    """First '/' segment of a value_path ('/other/x' -> 'other', 'foo..bar/x' -> 'foo..bar', 'value' -> 'value')."""
    return value_path.lstrip("/").split("/", 1)[0]


#: What follows an agent in the path of one of its assets (``self.assets.scope``, ``deps.robot.assets.arm``).
BLOK_ASSETS_SEGMENT = "assets"


def blok_asset_path(path: str) -> tuple[str, str] | None:
    """The ``(dependency key, asset name)`` of ``self.assets.<name>`` or ``deps.<key>.assets.<name>``, or None if ``path`` is not exactly that."""
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "self" and parts[1] == BLOK_ASSETS_SEGMENT and parts[2]:
        return "self", parts[2]
    if len(parts) == 4 and parts[0] == "deps" and parts[1] and parts[2] == BLOK_ASSETS_SEGMENT and parts[3]:
        return parts[1], parts[3]
    return None


def _resolve_port_path(path: str, ports: list["PortInputModel"]) -> bool:
    """True if a port path ('a..b..c') resolves through ``children`` from the given root ports."""
    candidates: list[PortInputModel] = ports
    for segment in path.split(PORT_PATH_SEPARATOR):
        match = next((port for port in candidates if port.key == segment), None)
        if match is None:
            return False
        candidates = match.children or []
    return True


def _check_keyed(arguments: Optional[List["ActionArgumentInputModel"]], owner: str) -> None:
    """Map-shaped argument lists (call arguments, value_dict) need unique, non-empty keys."""
    seen: set[str] = set()
    for argument in arguments or []:
        if not argument.key:
            raise ValueError(f"{owner}: every entry must carry a key")
        if argument.key in seen:
            raise ValueError(f"{owner}: duplicate key {argument.key!r}")
        seen.add(argument.key)


def iter_util_calls(arguments: Optional[List["ActionArgumentInputModel"]]) -> Iterator["UtilCallInputModel"]:
    """Every UtilCall nested anywhere inside an argument tree (depth first)."""
    for argument in arguments or []:
        if argument.util_call is not None:
            yield argument.util_call
            yield from iter_util_calls(argument.util_call.arguments)
        if argument.agent_call is not None:
            yield from iter_util_calls(argument.agent_call.arguments)
        yield from iter_util_calls(argument.value_list)
        yield from iter_util_calls(argument.value_dict)


def iter_component_nodes(components: Optional[List["ComponentNodeInputModel"]]) -> Iterator["ComponentNodeInputModel"]:
    """Every node of a component tree (pre-order)."""
    for node in components or []:
        yield node
        yield from iter_component_nodes(node.children)


def _check_pure_call(call: "UtilCallInputModel", dependencies: list[str] | None, owner: str, *, extra_roots: tuple[str, ...] = ()) -> None:
    """Enforce that a port call is pure and only references declared dependencies.

    A port call (effect, validator, widget or optimistic pointer) is evaluated client-side against
    the blok catalog. It must not trigger agent interactions, and every ``value_path`` in its
    argument tree must resolve to a name in ``dependencies``, to ``value`` (the port's own value)
    or to one of ``extra_roots`` (e.g. ``state`` for state widgets, ``args`` for optimistics).
    """
    allowed = set(dependencies or []) | {"value"} | set(extra_roots)

    def walk(arguments: Optional[List["ActionArgumentInputModel"]]) -> None:
        for argument in arguments or []:
            # A port call is held to base@1, as before blok language v2: its literal bool is the
            # int it always was (pydantic's lax mode), so the definition hashes as it did.
            if isinstance(argument.value_literal, bool):
                argument.value_literal = int(argument.value_literal)
            if argument.agent_call is not None:
                raise ValueError(f"{owner} must be pure: nested agent calls are not allowed")
            if argument.value_path is not None:
                root = _value_path_root(argument.value_path)
                if root not in allowed:
                    raise ValueError(f"{owner} references '{root}' via value_path but it is not in dependencies")
            if argument.util_call is not None:
                walk(argument.util_call.arguments)
            walk(argument.value_list)
            walk(argument.value_dict)

    walk(call.arguments)


class EffectInputModel(BaseModel):
    call: "UtilCallInputModel" = Field(description="The pure blok UtilCall, evaluated client-side against the catalog, that decides whether the effect applies. It must return a boolean. Argument value_paths may only reference names listed in `dependencies`, plus `value` for the port's own value.")
    dependencies: list[str] | None = Field(
        default_factory=list,
        description="The form-field subscription list of the effect: the keys of the other ports whose values the call may reference. This list is authoritative: a value_path in the call may only reference these names (plus `value` for the port's own value). Use the .. syntax to traverse the tree of ports, e.g. 'foo..bar' for the child 'bar' of port 'foo'.",
    )
    message: str | None = Field(default=None, description="The message to display when the effect is applied (if it is a message effect)")
    kind: enums.EffectKind = Field(description="The kind of the effect. Can be either message, hide or custom")
    fade: bool = Field(default=True, description="Whether to fade out the port when the effect is applied (if it is a hide effect)")
    source: str | None = Field(default=None, description="The authoring expression the call was compiled from (informational; never parsed or validated by the server).")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_call_is_pure(self) -> Self:
        """Reject impure calls and value_paths outside the declared dependencies."""
        _check_pure_call(self.call, self.dependencies, f"Effect {self.kind.value} ({self.call.operation})")
        return self

    @model_validator(mode="after")
    def check_kind_fields(self) -> Self:
        """A MESSAGE effect needs its message; `fade` only means something on HIDE."""
        if self.kind == enums.EffectKind.MESSAGE and not self.message:
            raise ValueError("MESSAGE effect requires a message")
        if self.kind != enums.EffectKind.HIDE and "fade" in self.model_fields_set:
            raise ValueError(f"{self.kind.value} effect must not set fade (HIDE only)")
        return self


class ChoiceInputModel(BaseModel):
    value: Any = Field(description="The value of the choice (any JSON value); must fit the port's kind. This is the value that is returned when the choice is selected")
    label: str = Field(description="The label of the choice. This is the text that is displayed in the UI")
    image: str | None = Field(default=None, description="The image of the choice. This is the image that is displayed in the UI (must be a URL)")
    description: str | None = Field(default=None, description="The description of the choice. This is the text that is displayed in the UI when the user hovers over the choice")

    model_config = ConfigDict(extra="forbid")


class ValidatorInputModel(BaseModel):
    call: "UtilCallInputModel" = Field(
        description="The pure blok UtilCall, evaluated client-side against the catalog, that validates the port value. It must return a boolean meaning 'valid'. Argument value_paths may only reference names listed in `dependencies`, plus `value` for the port's own value."
    )
    dependencies: list[str] | None = Field(
        default_factory=list,
        description="The form-field subscription list of the validator: the keys of the other ports whose values the call may reference. This list is authoritative: a value_path in the call may only reference these names (plus `value` for the port's own value). Use the .. syntax to traverse the tree of ports, e.g. 'foo..bar' for the child 'bar' of port 'foo'.",
    )
    label: str | None = Field(default=None, description="An optional human-readable label for the validator.")
    error_message: str | None = Field(default=None, description="The error message to display when the validation fails")
    source: str | None = Field(default=None, description="The authoring expression the call was compiled from (informational; never parsed or validated by the server).")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_call_is_pure(self) -> Self:
        """Reject impure calls and value_paths outside the declared dependencies."""
        _check_pure_call(self.call, self.dependencies, f"Validator {self.label or self.call.operation}")
        return self


class StateAccessorInputModel(BaseModel):
    option_key: enums.OptionKey = Field(description="The part of the state accessor to use as the value for the assign widget (e.g. the key, the description, the logo, etc.)")
    path: str | None = Field(default=None, description="Static JSON pointer into the state value ('/x/y'). Omit for the whole value. Mutually exclusive with `call`.")
    call: Optional["UtilCallInputModel"] = Field(default=None, description="Pure UtilCall returning the pointer string dynamically. May reference `state`, `value` and the widget's `dependencies`. Mutually exclusive with `path`.")

    @model_validator(mode="after")
    def check_one_of(self) -> Self:
        """A pointer is either static or computed, not both."""
        if self.path is not None and self.call is not None:
            raise ValueError("StateAccessor: set either path or call, not both")
        return self


def _as_widget_prop(prop: "ComponentPropInputModel", owner: str) -> None:
    """A widget's prop is held to base@1, as before blok language v2: its widget feeds the
    definition's hash. A static value is a string, number or object (a bool is its int, as it
    always was); it is bound one way or declares a value."""
    if isinstance(prop.static_value, bool):
        prop.static_value = int(prop.static_value)
    elif isinstance(prop.static_value, list):
        raise ValueError(f"{owner}: static_value must be a string, number or dict")
    bound = [name for name in ("static_value", "dynamic_value", "agent_call", "util_call") if getattr(prop, name) is not None]
    if not bound and not prop.declares_value:
        raise ValueError(f"ComponentProp {prop.key!r} is neither bound nor declares a value")


def _check_widget_props(props: Optional[List["ComponentPropInputModel"]], dependencies: list[str] | None, owner: str) -> None:
    """Custom widget props: no agent calls; value_paths only reference `value` and `dependencies`."""
    allowed = set(dependencies or []) | {"value"}
    for prop in props or []:
        prop_owner = f"{owner} prop {prop.key!r}"
        _as_widget_prop(prop, prop_owner)
        if prop.agent_call is not None:
            raise ValueError(f"{prop_owner} must be pure: agent calls are not allowed in widgets")
        if prop.dynamic_value is not None and prop.dynamic_value.path is not None:
            root = _value_path_root(prop.dynamic_value.path)
            if root not in allowed:
                raise ValueError(f"{prop_owner} references '{root}' via dynamic_value.path but it is not in dependencies")
        if prop.util_call is not None:
            _check_pure_call(prop.util_call, dependencies, prop_owner)


def _as_plain(value: Any) -> Any:
    """A discriminated-union field accepts dicts (matched on `kind`) or its own member instances.

    Strawberry hands us member model instances (via the merged input's ``to_pydantic``), config
    files hand us dicts. Dumping instances keeps both paths identical and makes a foreign model
    instance a clean validation error instead of a type mismatch.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, (list, tuple)):
        return [_as_plain(item) for item in value]
    return value


# ----------------------------------------------------------------------------
# Assign widgets: one model per kind, discriminated by ``kind``. ``extra="forbid"`` is what makes
# a field of another kind an error instead of a silent drop (kante's merged input relies on it).
# ----------------------------------------------------------------------------
class _AssignWidgetBase(BaseModel):
    follow_value: str | None = Field(default=None, description="Port path of another port whose value this widget follows and mirrors.")

    model_config = ConfigDict(extra="forbid")


class SliderAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["SLIDER"] = Field(description="Which member of AssignWidgetInput this is.")
    min: float | None = Field(default=None, description="The minimum value.")
    max: float | None = Field(default=None, description="The maximum value.")
    step: float | None = Field(default=None, description="The step between selectable values; must be positive.")

    @model_validator(mode="after")
    def check_range(self) -> Self:
        """A slider's range must be non-empty and its step positive."""
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError(f"SLIDER widget: min ({self.min}) must be smaller than max ({self.max})")
        if self.step is not None and self.step <= 0:
            raise ValueError(f"SLIDER widget: step must be positive, got {self.step}")
        return self


class ChoiceAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["CHOICE"] = Field(description="Which member of AssignWidgetInput this is.")
    placeholder: str | None = Field(default=None, description="The placeholder text shown before a choice is made. The choices themselves are the port's `choices`.")


class StringAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["STRING"] = Field(description="Which member of AssignWidgetInput this is.")
    placeholder: str | None = Field(default=None, description="The placeholder text.")
    as_paragraph: bool | None = Field(default=None, description="Render as a multi-line paragraph.")


SEARCH_QUERY_VARIABLES = ("search", "values")


class SearchAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["SEARCH"] = Field(description="Which member of AssignWidgetInput this is.")
    query: str = Field(description="The GraphQL query the ward executes to populate the choices. Must be a single `query` operation declaring `$search: String` and `$values: [ID!]`, plus one variable per filter port key.")
    ward: str = Field(min_length=1, description="The ward (service) that executes the query.")
    filters: list["ArgPortInputModel"] | None = Field(default=None, description="Filter ports whose values are passed to the query as variables named by their keys.")
    dependencies: list[str] | None = Field(default_factory=list, description="The other ports (port paths, `..` traverses children) whose values the query may reference.")
    placeholder: str | None = Field(default=None, description="The placeholder text.")

    @model_validator(mode="after")
    def check_query(self) -> Self:
        """The query parses, is one `query` operation, and declares the variables the widget will pass."""
        from graphql import OperationType, parse
        from graphql.error import GraphQLSyntaxError

        try:
            document = parse(self.query)
        except GraphQLSyntaxError as error:
            raise ValueError(f"SEARCH widget query does not parse: {error.message}") from error

        operations = [definition for definition in document.definitions if getattr(definition, "operation", None) is not None]
        if len(operations) != 1 or operations[0].operation != OperationType.QUERY:
            raise ValueError("SEARCH widget query must contain exactly one `query` operation")
        declared = {variable.variable.name.value for variable in operations[0].variable_definitions or []}
        missing = sorted(set(SEARCH_QUERY_VARIABLES) - declared)
        if missing:
            raise ValueError(f"SEARCH widget query must declare the variables {['$' + name for name in missing]}")

        keys = [port.key for port in self.filters or []]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"SEARCH widget filters have duplicate keys {duplicates}")
        if "value" in keys:
            raise ValueError("SEARCH widget filters may not use the reserved key 'value'")
        if "requires" in keys:
            raise ValueError("SEARCH widget filters may not use the reserved key 'requires': a frontend passes the port's own constraints under it")
        undeclared = sorted(set(keys) - declared)
        if undeclared:
            raise ValueError(f"SEARCH widget query must declare a variable for each filter port: missing {['$' + key for key in undeclared]}")
        return self


class CustomAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["CUSTOM"] = Field(description="Which member of AssignWidgetInput this is.")
    component: str = Field(min_length=1, description="The catalog component to render. The port value is in scope as the reserved root `value`.")
    props: Optional[List["ComponentPropInputModel"]] = Field(default=None, description="Props of the component. value_paths may only reference `value` and `dependencies`; agent calls are not allowed.")
    dependencies: list[str] | None = Field(default_factory=list, description="The other ports (port paths, `..` traverses children) whose values the props may reference.")
    fallback: Optional["AssignWidgetInputModel"] = Field(default=None, description="Widget to render when the UI has no such component in its catalog.")

    @field_validator("fallback", mode="before")
    @classmethod
    def _plain_fallback(cls, value: Any) -> Any:
        return _as_plain(value)

    @model_validator(mode="after")
    def check_props(self) -> Self:
        """Props are pure and only reference declared ports."""
        _check_widget_props(self.props, self.dependencies, "CustomAssignWidget")
        return self


class StateChoiceAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["STATE_CHOICE"] = Field(description="Which member of AssignWidgetInput this is.")
    dependency: str | None = Field(default=None, description="The agent dependency (by key) whose state provides the choices; omitted: the implementing agent's own state.")
    state_path: str | None = Field(default=None, description="Static JSON pointer into the state value that provides the choices. Mutually exclusive with `state_call`.")
    state_call: Optional["UtilCallInputModel"] = Field(default=None, description="Pure UtilCall returning that pointer dynamically; may reference `state`, `value` and `dependencies`. Mutually exclusive with `state_path`.")
    state_accessors: list[StateAccessorInputModel] | None = Field(default=None, description="How to read label/description/logo/value out of each state entry; each accessor is a static pointer or a pure call.")
    dependencies: list[str] | None = Field(default_factory=list, description="The other ports (port paths, `..` traverses children) whose values the calls may reference.")

    @model_validator(mode="after")
    def check_pointer(self) -> Self:
        """Exactly one pointer source, and every call is pure."""
        if (self.state_path is None) == (self.state_call is None):
            raise ValueError("STATE_CHOICE widget needs exactly one of state_path or state_call")
        if self.state_call is not None:
            _check_pure_call(self.state_call, self.dependencies, "StateChoice state_call", extra_roots=("state",))
        for index, accessor in enumerate(self.state_accessors or []):
            if accessor.call is not None:
                _check_pure_call(accessor.call, self.dependencies, f"StateAccessor {index}", extra_roots=("state",))
        return self


class ProxyAssignWidgetInputModel(_AssignWidgetBase):
    kind: Literal["PROXY"] = Field(description="Which member of AssignWidgetInput this is.")
    target_port: str = Field(min_length=1, description="The port key on the targeted action.")
    target_action: str = Field(min_length=1, description="The action to target: an action-dependency key of `target_dependency` when that is set.")
    target_dependency: str | None = Field(default=None, description="The agent dependency (by key) that provides the targeted action; omitted: the implementing agent itself.")


AssignWidgetInputModel = Annotated[
    Union[
        SliderAssignWidgetInputModel,
        ChoiceAssignWidgetInputModel,
        StringAssignWidgetInputModel,
        SearchAssignWidgetInputModel,
        CustomAssignWidgetInputModel,
        StateChoiceAssignWidgetInputModel,
        ProxyAssignWidgetInputModel,
    ],
    Field(discriminator="kind"),
]
"""An assign widget of any kind, discriminated by ``kind``."""

ASSIGN_WIDGET_INPUT_MODELS: dict[str, type[_AssignWidgetBase]] = {
    "SLIDER": SliderAssignWidgetInputModel,
    "CHOICE": ChoiceAssignWidgetInputModel,
    "STRING": StringAssignWidgetInputModel,
    "SEARCH": SearchAssignWidgetInputModel,
    "CUSTOM": CustomAssignWidgetInputModel,
    "STATE_CHOICE": StateChoiceAssignWidgetInputModel,
    "PROXY": ProxyAssignWidgetInputModel,
}


# ----------------------------------------------------------------------------
# Return widgets
# ----------------------------------------------------------------------------
class _ReturnWidgetBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceReturnWidgetInputModel(_ReturnWidgetBase):
    kind: Literal["CHOICE"] = Field(description="Which member of ReturnWidgetInput this is. Displays the port's `choices`.")


class CustomReturnWidgetInputModel(_ReturnWidgetBase):
    kind: Literal["CUSTOM"] = Field(description="Which member of ReturnWidgetInput this is.")
    component: str = Field(min_length=1, description="The catalog component to render. The returned value is in scope as the reserved root `value`.")
    props: Optional[List["ComponentPropInputModel"]] = Field(default=None, description="Props of the component; value_paths may only reference `value`, agent calls are not allowed.")

    @model_validator(mode="after")
    def check_props(self) -> Self:
        """Props are pure and only reference the returned value."""
        _check_widget_props(self.props, None, "CustomReturnWidget")
        return self


ReturnWidgetInputModel = Annotated[
    Union[ChoiceReturnWidgetInputModel, CustomReturnWidgetInputModel],
    Field(discriminator="kind"),
]
"""A return widget of any kind, discriminated by ``kind``."""

RETURN_WIDGET_INPUT_MODELS: dict[str, type[_ReturnWidgetBase]] = {
    "CHOICE": ChoiceReturnWidgetInputModel,
    "CUSTOM": CustomReturnWidgetInputModel,
}


def iter_widget_chain(widget: Any) -> Iterator[Any]:
    """A widget followed by its fallback chain."""
    while widget is not None:
        yield widget
        widget = getattr(widget, "fallback", None)


# Which port kinds each assign widget kind can edit. Absent kinds fit any port.
_WIDGET_PORT_KINDS: dict[str, set[enums.PortKind]] = {
    "SLIDER": {enums.PortKind.INT, enums.PortKind.FLOAT, enums.PortKind.QUANTITY},
    "STRING": {enums.PortKind.STRING},
    "SEARCH": {enums.PortKind.STRUCTURE, enums.PortKind.MEMORY_STRUCTURE, enums.PortKind.LIST},
}


def _check_widget_fits_port(widget: Any, port: "PortInputModel") -> None:
    """A widget only makes sense on ports whose kind it can edit; choices and defaults must agree."""
    fits = _WIDGET_PORT_KINDS.get(widget.kind)
    if fits is not None and port.kind not in fits:
        raise ValueError(f"Port {port.key!r} of kind {port.kind.value} cannot use a {widget.kind} widget (fits {sorted(kind.value for kind in fits)})")
    if widget.kind == "SEARCH" and port.kind == enums.PortKind.LIST:
        child = (port.children or [None])[0]
        if child is None or child.kind not in (enums.PortKind.STRUCTURE, enums.PortKind.MEMORY_STRUCTURE):
            raise ValueError(f"Port {port.key!r}: a SEARCH widget on a LIST port needs a STRUCTURE child")
    if widget.kind == "CHOICE" and not port.choices:
        raise ValueError(f"Port {port.key!r}: a CHOICE widget needs the port to declare `choices`")
    default = getattr(port, "default", None)
    if widget.kind == "SLIDER" and isinstance(default, (int, float)) and not isinstance(default, bool):
        below = widget.min is not None and default < widget.min
        above = widget.max is not None and default > widget.max
        if below or above:
            raise ValueError(f"Port {port.key!r}: default {default} lies outside the SLIDER range [{widget.min}, {widget.max}]")


def _check_default_in_choices(port: "PortInputModel") -> None:
    default = getattr(port, "default", None)
    if port.choices and default is not None:
        values = [choice.value for choice in port.choices]
        if default not in values:
            raise ValueError(f"Port {port.key!r}: default {default!r} is not one of its choices {values}")


def _check_descriptor(descriptor: "RequiresInputModel | ProvidesInputModel", owner: str) -> None:
    """The operator and the value agree: IN/NOT_IN take a list, LTE/GTE a number, EXISTS no value or a boolean."""
    operator, value = descriptor.operator, descriptor.value
    if operator in (enums.DescriptorOperator.IN, enums.DescriptorOperator.NOT_IN) and not isinstance(value, list):
        raise ValueError(f"{owner} {descriptor.key!r}: {operator.value} needs a list value")
    if operator in (enums.DescriptorOperator.LTE, enums.DescriptorOperator.GTE) and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"{owner} {descriptor.key!r}: {operator.value} needs a numeric value")
    if operator == enums.DescriptorOperator.EXISTS and value is not None and not isinstance(value, bool):
        raise ValueError(f"{owner} {descriptor.key!r}: EXISTS takes no value or a boolean")


class RequiresInputModel(BaseModel):
    key: str = Field(min_length=1, description="The key of the requirement: the descriptor name the constraint reads, matched verbatim as one flat key of the candidate object (any non-empty string, e.g. 'axes' or '@mikro/n_space_axes')")
    operator: enums.DescriptorOperator = Field(description="The operator for the requirement")
    value: Any = Field(default=None, description="The value of the requirement. This can be any JSON serializable value; IN/NOT_IN take a list, LTE/GTE a number, EXISTS none")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_operator_value(self) -> Self:
        """Operator and value agree."""
        _check_descriptor(self, "requires")
        return self


class ProvidesInputModel(BaseModel):
    key: str = Field(min_length=1, description="The key of the provision: the descriptor name the constraint reads, matched verbatim as one flat key of the candidate object (any non-empty string, e.g. 'axes' or '@mikro/n_space_axes')")
    operator: enums.DescriptorOperator = Field(description="The operator for the provision")
    value: Any = Field(default=None, description="The value of the provision. This can be any JSON serializable value; IN/NOT_IN take a list, LTE/GTE a number, EXISTS none")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_operator_value(self) -> Self:
        """Operator and value agree."""
        _check_descriptor(self, "provides")
        return self


class OptimisticInputModel(BaseModel):
    state: str = Field(description="The state to optimistically set when the action is assigned")
    path: str | None = Field(default=None, description="Static JSON pointer into the state value to set. Mutually exclusive with `path_call`.")
    path_call: Optional["UtilCallInputModel"] = Field(default=None, description="Pure UtilCall returning the pointer dynamically; may reference `args` (the assignment arguments). Mutually exclusive with `path`.")
    accessor: str | None = Field(default=None, description="Static JSON pointer into the assignment args for the value to set; omitted: the whole args.")

    @model_validator(mode="after")
    def check_one_of(self) -> Self:
        """The pointer is either static or computed, and a computed one only sees the args."""
        if (self.path is None) == (self.path_call is None):
            raise ValueError("Optimistic needs exactly one of path or path_call")
        if self.path_call is not None:
            _check_pure_call(self.path_call, [], f"Optimistic {self.state}", extra_roots=("args",))
        return self


IDENTIFIER_PATTERN = re.compile(r"^@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
"""A structure identifier: ``@package/key``, e.g. ``@mikro/image``."""

_IDENTIFIED_KINDS = {enums.PortKind.STRUCTURE, enums.PortKind.MEMORY_STRUCTURE, enums.PortKind.INTERFACE}
"""Kinds that must declare an identifier: it is the only identity the value has."""
_IDENTIFIABLE_KINDS = {enums.PortKind.MODEL, enums.PortKind.ENUM}
"""Kinds that may declare one: a MODEL naming its class, an ENUM naming the enum its choices came from."""
_CHILD_COUNT: dict[enums.PortKind, tuple[int, int | None]] = {
    enums.PortKind.LIST: (1, 1),
    enums.PortKind.DICT: (1, None),
    enums.PortKind.UNION: (2, None),
    enums.PortKind.MODEL: (1, None),
}
_CHOICE_KINDS = {enums.PortKind.ENUM, enums.PortKind.INT, enums.PortKind.FLOAT, enums.PortKind.STRING}


def _check_port_shape(port: "PortInputModel") -> None:
    """The per-kind structure table: children, identifier and choices."""
    owner = f"Port {port.key!r} of kind {port.kind.value}"
    children = port.children or []

    bounds = _CHILD_COUNT.get(port.kind)
    if bounds is None:
        if children:
            raise ValueError(f"{owner} must not have children")
    else:
        low, high = bounds
        if len(children) < low or (high is not None and len(children) > high):
            expected = "exactly one child" if (low, high) == (1, 1) else f"at least {low} children"
            raise ValueError(f"{owner} must have {expected}, got {len(children)}")
        _check_unique(children, "key", f"{owner} children")
        if port.kind == enums.PortKind.DICT and len(children) > 1 and any(child.key == ITEM_KEY for child in children):
            raise ValueError(f"{owner}: a DICT is either homogeneous (one child keyed {ITEM_KEY!r}) or has named children, not both")

    if port.kind in _IDENTIFIED_KINDS:
        if not port.identifier:
            raise ValueError(f"{owner} must declare an identifier (@package/key)")
        if not IDENTIFIER_PATTERN.match(port.identifier):
            raise ValueError(f"{owner}: identifier {port.identifier!r} is not of the form @package/key")
    elif port.identifier is not None and port.kind not in _IDENTIFIABLE_KINDS:
        raise ValueError(f"{owner} must not declare an identifier")
    elif port.identifier is not None and not IDENTIFIER_PATTERN.match(port.identifier):
        raise ValueError(f"{owner}: identifier {port.identifier!r} is not of the form @package/key")

    if port.kind == enums.PortKind.ENUM and not port.choices:
        raise ValueError(f"{owner} must declare choices")
    if port.choices and port.kind not in _CHOICE_KINDS:
        raise ValueError(f"{owner} must not declare choices (only {sorted(kind.value for kind in _CHOICE_KINDS)} may)")
    if port.choices:
        _check_unique(port.choices, "value", f"{owner} choices")


class PortInputModel(BaseModel):
    key: str = Field(description="The key of the port: unique among its siblings, free of '..', not 'value'. LIST/DICT item ports are conventionally keyed '...'.")
    label: str | None = Field(default=None, description="The label of the port. This is the text that is displayed in the UI")
    kind: enums.PortKind = Field(description="The kind of the port. This is the type of the port. Can be either int, string, structure, list, bool, dict, float, date, union or model")
    description: str | None = Field(default=None, description="The description of the port. This is the text that is displayed in the UI when the user hovers over the port")
    identifier: str | None = Field(default=None, description="The identifier of the port's type, of the form @package/key. Required for STRUCTURE, MEMORY_STRUCTURE and INTERFACE, where it is the only identity a value has; optional for MODEL and ENUM, where it names the class or enum the port was built from so that agents can map a value back to it.")
    nullable: bool = Field(default=False, description="Whether the port is nullable or not. If the port is nullable, it can be set to null. If the port is not nullable, it cannot be set to null")
    effects: list[EffectInputModel] | None = Field(default=None, description="The effects of the port")
    choices: list[ChoiceInputModel] | None = Field(default=None, description="The values the port accepts (required for ENUM; optional for INT, FLOAT, STRING). Rendered by CHOICE widgets.")
    reference_unit: str | None = Field(
        default=None, description='For QUANTITY ports: the canonical/reference unit of the physical quantity, e.g. "volt" or "farad". It is the default selection and the key used to resolve the concrete quantity type; other units of the same dimension are still allowed.'
    )
    proposed_units: list[str] | None = Field(default=None, description='For QUANTITY ports: units offered as a dropdown in the UI, e.g. ["pF", "nF", "uF"]. Proposals only — any unit of the same dimension remains valid input.')
    dimension: str | None = Field(default=None, description='For QUANTITY ports: the pint dimensionality string, e.g. "[mass] * [length] ** 2 / [time] ** 3 / [current]". This is the wiring-compatibility key between quantity ports.')
    children: Optional[list["PortInputModel"]] = Field(default=None, description="The child ports: the item type of a LIST or DICT, the variants of a UNION, the fields of a MODEL.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_kind_specific_fields(self) -> Self:
        """Key, per-kind structure (children, identifier, choices) and the QUANTITY unit rules."""
        _check_port_key(self.key, "Port")
        _check_port_shape(self)

        if self.kind == enums.PortKind.QUANTITY:
            if not self.reference_unit:
                raise ValueError(f"QUANTITY port '{self.key}' must declare a reference_unit")
            derived = units.dimensionality_of(self.reference_unit)
            if self.dimension is not None and units.dimensionality_of(self.dimension) != derived:
                raise ValueError(f"QUANTITY port '{self.key}': dimension '{self.dimension}' is inconsistent with reference_unit '{self.reference_unit}' (dimensionality '{derived}')")
            self.dimension = derived  # derive or canonicalize the wiring-compatibility key
            for unit in self.proposed_units or []:
                unit_dim = units.dimensionality_of(unit)
                if unit_dim != derived:
                    raise ValueError(f"QUANTITY port '{self.key}': proposed unit '{unit}' has dimensionality '{unit_dim}', expected '{derived}'")
        else:
            offending = [f for f in ("reference_unit", "proposed_units", "dimension") if getattr(self, f) is not None]
            if offending:
                raise ValueError(f"Port '{self.key}' of kind {self.kind.value} must not set QUANTITY-only fields: {', '.join(offending)}")
        return self


class ArgPortInputModel(PortInputModel):
    validators: list[ValidatorInputModel] | None = Field(default=None, description="The validators for the port")
    default: Any | None = Field(default=None, description="The default value for the port; must fit the port's kind.")
    widget: Optional["AssignWidgetInputModel"] = Field(default=None, description="The assign widget to use for this port, discriminated by `kind`.")
    requires: list[RequiresInputModel] | None = Field(default=None, description="The descriptors for the port. Descriptors are key-value pairs that can be used to add additional metadata to a port. When using rekuest's action search, you can filter actions based on their port descriptors")
    children: Optional[list["ArgPortInputModel"]] = Field(default=None, description="The child ports (used for list, dict, union and model ports).")

    @field_validator("widget", mode="before")
    @classmethod
    def _plain_widget(cls, value: Any) -> Any:
        return _as_plain(value)

    @model_validator(mode="after")
    def check_widget_and_default(self) -> Self:
        """The widget (and its fallbacks) fit the port's kind; a default fits the kind and the choices."""
        if self.default is not None:
            mismatch = value_mismatch(self, self.default)
            if mismatch:
                raise ValueError(f"Port default {mismatch}")
        _check_default_in_choices(self)
        for widget in iter_widget_chain(self.widget):
            _check_widget_fits_port(widget, self)
        return self


class ReturnPortInputModel(PortInputModel):
    widget: Optional["ReturnWidgetInputModel"] = Field(default=None, description="The return widget to use for this port, discriminated by `kind`.")
    provides: list[ProvidesInputModel] | None = Field(default=None, description="The provisions for the port. Provisions are key-value pairs that can be used to add additional metadata to a port. When using rekuest's action search, you can filter actions based on their port provisions")
    children: Optional[list["ReturnPortInputModel"]] = Field(default=None, description="The child ports (used for list, dict, union and model ports).")

    @field_validator("widget", mode="before")
    @classmethod
    def _plain_widget(cls, value: Any) -> Any:
        return _as_plain(value)

    @model_validator(mode="after")
    def check_widget_and_choices(self) -> Self:
        """A CHOICE return widget displays the port's choices, so the port must declare them."""
        if self.widget is not None and self.widget.kind == "CHOICE" and not self.choices:
            raise ValueError(f"Port {self.key!r}: a CHOICE return widget needs the port to declare `choices`")
        return self


class PortGroupInputModel(BaseModel):
    key: str = Field(min_length=1, description="The key of the port group. This is used to uniquely identify the port group")
    title: str | None = Field(default=None, description="The title of the port group. This is the text that is displayed in the UI")
    description: str | None = Field(default=None, description="The description of the port group. This is the text that is displayed in the UI")
    effects: list[EffectInputModel] | None = Field(default=None, description="The effects applied to the port group as a whole.")
    ports: list[str] = Field(default_factory=list, description="The keys of the root arg ports that belong to this group; a port belongs to at most one group.")

    model_config = ConfigDict(extra="forbid")


class DescriptorInputModel(BaseModel):
    key: str = Field(description="The descriptor key, e.g. 'axes'.")
    value: Any = Field(description="The descriptor value. Any JSON-serializable value.")


class PortMatchInputModel(BaseModel):
    at: int | None = Field(default=None, description="The index of the port to match.")
    key: str | None = Field(default=None, description="The key of the port to match.")
    kind: enums.PortKind | None = Field(default=None, description="The kind of the port to match.")
    identifier: str | None = Field(default=None, description="The identifier of the port to match.")
    nullable: bool | None = Field(default=None, description="Whether the port is nullable.")
    dimension: str | None = Field(default=None, description="The canonical pint dimensionality the port must have (QUANTITY wiring-compatibility key).")
    descriptors: list[DescriptorInputModel] | None = Field(default=None, description="Runtime descriptors of a candidate object, evaluated against the port's compiled requires micro-constraint. Omit for purely structural matching.")
    children: Optional[list["PortMatchInputModel"]] = Field(default=None, description="The matches for the children of the port to match.")


class ActionDemandInputModel(BaseModel):
    """Pure matching criteria for an action — the single demand shape used by query filters
    and (wrapped in a dependency) by dependency declarations.

    The preferred identification is ``app`` + ``key`` (e.g. "imagej" / "open_image"); the
    structural matches describe what the action must look like, so a resolver (or the user,
    when assigning) can progressively loosen the demand to equivalent actions of other apps.
    """

    hash: str | None = Field(default=None, description="The exact hash of the action. When set, matching short-circuits on the hash and everything else is ignored.")
    key: str | None = Field(default=None, description="The action's key within its app, e.g. 'open_image'. Together with `app` this is the preferred identification of the demanded action.")
    app: str | None = Field(default=None, description="The identifier of the app providing the action, e.g. 'imagej'. Omit (or drop when loosening) to allow equivalent actions from any app.")
    version: str | None = Field(default=None, description="The exact version of the action.")
    name: str | None = Field(default=None, description="The display name of the action to match.")
    arg_matches: list[PortMatchInputModel] | None = Field(default=None, description="The matches the action's arg ports must satisfy.")
    return_matches: list[PortMatchInputModel] | None = Field(default=None, description="The matches the action's return ports must satisfy.")
    protocols: list[str] | None = Field(default=None, description="Protocols (by name) the action must implement, e.g. 'predicate'.")
    force_arg_length: int | None = Field(default=None, description="Require that the action has exactly this number of root args.")
    force_return_length: int | None = Field(default=None, description="Require that the action has exactly this number of root returns.")
    pure: bool | None = Field(default=None, description="Require the action to be (or not be) pure. Omit to match either.")
    idempotent: bool | None = Field(default=None, description="Require the action to be (or not be) idempotent. Omit to match either.")
    stateful: bool | None = Field(default=None, description="Require the action to be (or not be) stateful. Omit to match either.")


class StateDemandInputModel(BaseModel):
    """Pure matching criteria for a state — the single state demand shape.

    The preferred identification is ``app`` + ``key`` (matched against the State's own
    identity columns); the structural ``matches`` on the state definition's ports loosen
    the demand to equivalent states of other apps.
    """

    hash: str | None = Field(default=None, description="The exact hash of the state definition. When set, matching short-circuits on the hash.")
    key: str | None = Field(default=None, description="The state's identity key on the agent (defaults to the interface at registration).")
    app: str | None = Field(default=None, description="The identifier of the app providing the state.")
    matches: list[PortMatchInputModel] | None = Field(default=None, description="The matches the state definition's ports must satisfy.")
    protocols: list[str] | None = Field(default=None, description="Protocols (by name) the state must implement.")


class ActionDependencyInputModel(BaseModel):
    """A named action requirement of a dependency: a local slot ``key`` mapped to the
    ``demand`` the resolved action must satisfy."""

    key: str = Field(description="The local slot key of this action requirement — callers reference it when assigning.")
    description: str | None = Field(default=None, description="The description of the dependency, why it is needed and what it is used for.")
    demand: ActionDemandInputModel | None = Field(default=None, description="The matching criteria the resolved action must satisfy (app/key preferred; matches loosen).")
    optional: bool = Field(default=False, description="Whether the dependency is optional or not. If the dependency is optional, the agent doesn't have to provide it to be potentially callable")
    allow_inactive: bool = Field(default=True, description="Allow inactive nodes, defaults to true")


class StateDependencyInputModel(BaseModel):
    """A named state requirement of a dependency: a local slot ``key`` mapped to the
    ``demand`` the agent's state must satisfy."""

    key: str = Field(description="The local slot key of this state requirement — callers reference it when assigning.")
    description: str | None = Field(default=None, description="The description of the dependency, why it is needed and what it is used for.")
    demand: StateDemandInputModel | None = Field(default=None, description="The matching criteria the agent's state must satisfy (app/key preferred; matches loosen).")
    optional: bool = Field(default=False, description="Whether the dependency is optional or not. If the dependency is optional, the agent doesn't have to provide it to be potentially callable")
    allow_inactive: bool = Field(default=True, description="Allow inactive nodes, defaults to true")


class AgentDependencyInputModel(BaseModel):
    key: str = Field(description="The key of this dependency, when assigning you can reference this key to specify which agent_dependency you are assigning to.")
    app: str | None = Field(
        default=None,
        description="Which app this dependency corresponds to (i.e. do you want to use a stardist agent for that or imagej agents needs to be a world unique classsifier (reverse domain notation) that identifies the type of agent you want to use, and then we can have multiple agents of the same type running in the system, e.g. startdist could be the app for all agents that correpsond to a startdist instance)",
    )
    version: str | None = Field(default=None, description="The version of the app this dependency corresponds to.")

    name: str | None = Field(default=None, description="The name of the agent. This is used to identify the agent in the system.")
    description: str | None = Field(default=None, description="A description of the dependency, why it is needed and what it is used for. This can be used to provide more context to users when assigning dependencies.")
    optional: bool = Field(default=False, description="Whether the dependency is optional or not. If the dependency is optional, users can choose to not provide it")

    # Filters for selecting which instances of the agent are valid for this dependency
    action_dependencies: list[ActionDependencyInputModel] | None = Field(default=None, description="The named action requirements of the agent — each a slot key plus the demand the resolved action must satisfy.")
    state_dependencies: list[StateDependencyInputModel] | None = Field(default=None, description="The named state requirements of the agent — each a slot key plus the demand the agent's state must satisfy.")
    auto_resolvable: bool = Field(
        default=False,
        description="Whether this dependency is auto resolvable or not. If so we will try to automatically resolve it based on the demands specified in the dependency and the capabilities of the available agents in the system. This is used to identify the demand in the system. Attention if any of the dependencies of this agent dependency is not auto resolvable, this dependency will also not be auto resolvable",
    )

    mutually_exclusive_keys: list[str] | None = Field(
        default=None, description="A list of keys of other agent dependencies that are mutually exclusive with this one. This means two agent dependencies with mutually exclusive keys cannot be assigned to the same implementing agent. This is used to identify the demand in the system."
    )
    min_viable_instances: int | None = Field(default=None, description="The minimum amount of viable instances for the agent. This is used to identify the demand in the system.")
    max_viable_instances: int | None = Field(default=None, description="The maximum amount of viable instances for the agent. This is used to identify the demand in the system.")
    prefered_instances: int | None = Field(default=None, description="The prefered amount of instances for the agent. This is used to identify the demand in the system.")


class TestTargetInputModel(BaseModel):
    """Identifies the action(s) a test action tests: by exact hash, or by an (app, key,
    version) coordinate. When ``key`` is used, ``app`` defaults to the registering agent's
    app and omitting ``version`` matches every version of that action."""

    hash: str | None = Field(default=None, description="The exact hash of the target action.")
    app: str | None = Field(default=None, description="The app identifier owning the target action. Defaults to the registering agent's app.")
    key: str | None = Field(default=None, description="The key of the target action. Matches every version unless version is given.")
    version: str | None = Field(default=None, description="Restrict a key target to one specific version.")

    @model_validator(mode="after")
    def check_target(self) -> Self:
        if not self.hash and not self.key:
            raise ValueError("A test target must provide either a hash or a key")
        return self


class DefinitionInputModel(BaseModel):
    """A definition for a implementation"""

    key: str = Field(description="The key of the definition. This is used to uniquely identify the definition")
    version: str = Field(default="1", description="The version of the definition. This is used to differentiate if the underyling algorithm has changed, i.e we would expect different results for the same input")
    description: str | None = Field(default=None, description="The description of the definition. This is the text that is displayed in the UI")
    collections: list[str] = Field(default_factory=list, description="The collections of the definition. This is used to group definitions together in the UI")
    name: str = Field(description="The name of the actions. This is used to uniquely identify the definition")
    stateful: bool = Field(default=False, description="Whether the definition is stateful or not. If the definition is stateful, it can be used to create a stateful action. If the definition is not stateful, it cannot be used to create a stateful action")
    pure: bool = Field(default=False, description="Whether the action is pure: same args always produce the same result and no side effects — its results are replayable/cacheable. Implies idempotent. Incompatible with stateful and with a PHYSICAL effect class.")
    idempotent: bool = Field(default=False, description="Whether the action is idempotent: safe to run multiple times with the same args without changing the outcome — on ambiguous executor loss it may be freely re-dispatched.")
    allow_probe: bool = Field(default=False, description="Whether the action may be invoked as a probe: zero persistence, redis-held state, no history/replay/recovery. Only actions declaring this are callable via the call mutation.")
    port_groups: list[PortGroupInputModel] = Field(default_factory=list, description="The port groups of the definition. This is used to group ports together in the UI")
    args: list[ArgPortInputModel] = Field(default_factory=list, description="The args of the definition. This is the input ports of the definition")
    returns: list[ReturnPortInputModel] = Field(default_factory=list, description="The returns of the definition. This is the output ports of the definition")
    kind: enums.ActionKind = Field(description="The kind of the definition. This is the type of the definition. Can be either a function or a generator")
    is_test_for: list[TestTargetInputModel] = Field(default_factory=list, description="The actions this definition is a test for, each identified by hash or by (app, key, version).")
    is_dev: bool = Field(default=False, description="Whether the definition is a dev definition or not. If the definition is a dev definition, it can be used to create a dev action. If the definition is not a dev definition, it cannot be used to create a dev action")

    catalogs: list[str] | None = Field(
        default_factory=list,
        description="Names of the UI catalogs (in the registering agent's organization) that extend the base catalog for this definition's effect and validator calls. The base catalog (`base@1`) is always applied and may be named explicitly; naming another base version or a catalog that does not exist yields an unknown_catalog warning. Two catalogs defining the same operation differently is a registration error. An operation no catalog provides is stored as a warning on the implementation.",
    )
    protocols: list[str] = Field(
        default_factory=list,
        description="Where the action means to hook in (`on_scene_annotation`): hints a caller filters on, stored beside the protocols the server infers. Not a contract -- nothing checks the ports against them -- and not part of the action's hash.",
    )
    bloks: list["ActionBlokInputModel"] = Field(
        default_factory=list,
        # Never dumped when empty: a definition without bloks is the bytes it was before actions
        # had any, which an older takt (`deny_unknown_fields`) or kabinet still reads.
        exclude_if=lambda bloks: not bloks,
        description="The action's own bloks, at most one per context: how a client shows a run of it (`html` in a task panel, `overlay` drawn into the space that hosts the run). They name devices only by the action's dependency keys. Stored with the action, never listed on their own, and not part of the action's hash.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_bloks(self) -> Self:
        """An action has one blok per context: the one a client picks for where it shows the run."""
        contexts = [blok.context for blok in self.bloks]
        twice = next((context for context in contexts if contexts.count(context) > 1), None)
        if twice is not None:
            raise ValueError(f"Definition {self.key}: the {twice.value.lower()} blok is given twice; an action has one blok per context")
        return self

    @model_validator(mode="after")
    def check_protocols(self) -> Self:
        """A protocol hint is a name to filter on: one word, said once."""
        for protocol in self.protocols:
            # `Protocol.name` is a CharField(max_length=1000).
            if not protocol or len(protocol) > 1000 or any(character.isspace() for character in protocol):
                raise ValueError(f"Definition {self.key}: a protocol hint is a name without whitespace, got {protocol!r}")
        twice = sorted({protocol for protocol in self.protocols if self.protocols.count(protocol) > 1})
        if twice:
            raise ValueError(f"Definition {self.key}: the protocol hint {twice[0]!r} is given twice")
        return self

    @model_validator(mode="after")
    def check_keys(self) -> Self:
        """Root arg keys and root return keys are unique, and the two sets do not overlap."""
        _check_unique(self.args, "key", f"Definition {self.key} args")
        _check_unique(self.returns, "key", f"Definition {self.key} returns")
        shared = sorted({port.key for port in self.args} & {port.key for port in self.returns})
        if shared:
            raise ValueError(f"Definition {self.key}: args and returns share the keys {shared}; dependency paths could not tell them apart")
        return self

    @model_validator(mode="after")
    def check_port_groups(self) -> Self:
        """Group keys are unique, every member is a root arg, and no port sits in two groups."""
        _check_unique(self.port_groups, "key", f"Definition {self.key} port_groups")
        arg_keys = {port.key for port in self.args}
        seen: dict[str, str] = {}
        for group in self.port_groups:
            for key in group.ports or []:
                if key not in arg_keys:
                    raise ValueError(f"Definition {self.key}: port group {group.key!r} lists unknown arg {key!r}")
                if key in seen:
                    raise ValueError(f"Definition {self.key}: arg {key!r} is in both port groups {seen[key]!r} and {group.key!r}")
                seen[key] = group.key
        return self

    @model_validator(mode="after")
    def check_dependencies(self) -> Self:
        """Every dependency of every validator, effect and widget (args, returns, nested children, port groups) is a resolvable port path."""
        roots: list[PortInputModel] = [*self.args, *self.returns]

        def check(dependencies: list[str] | None, owner: str) -> None:
            for dep in dependencies or []:
                if not _resolve_port_path(dep, roots):
                    raise ValueError(f"{owner} has invalid dependency: {dep}")

        def walk(ports: list[PortInputModel], prefix: str = "") -> None:
            for port in ports:
                path = f"{prefix}{port.key}"
                for validator in getattr(port, "validators", None) or []:
                    check(validator.dependencies, f"Validator {validator.label or validator.call.operation} in port {path}")
                for effect in port.effects or []:
                    check(effect.dependencies, f"Effect {effect.kind.value} ({effect.call.operation}) in port {path}")
                for depth, widget in enumerate(iter_widget_chain(getattr(port, "widget", None))):
                    owner = f"Widget {widget.kind} in port {path}" + (f" (fallback {depth})" if depth else "")
                    check(getattr(widget, "dependencies", None), owner)
                    follow = getattr(widget, "follow_value", None)
                    if follow is not None and not _resolve_port_path(follow, roots):
                        raise ValueError(f"{owner} follows an unknown port: {follow}")
                walk(port.children or [], f"{path}{PORT_PATH_SEPARATOR}")

        walk(self.args)
        walk(self.returns)
        for group in self.port_groups or []:
            for effect in group.effects or []:
                check(effect.dependencies, f"Effect {effect.kind.value} ({effect.call.operation}) in port group {group.key}")

        return self

    @property
    def unique_hash(self) -> str:
        """Stable sha256 over the identity-bearing subset of the definition (stored as Action.hash)."""
        hashable_definition = {
            key: value
            for key, value in dict(self.model_dump()).items()
            if key
            in [
                "name",
                "description",
                "args",
                "returns",
                "stateful",
                "is_test_for",
                "collections",
                "key",
                "version",
                "kind",
                "port_groups",
            ]
        }
        return hashlib.sha256(json.dumps(hashable_definition, sort_keys=True).encode()).hexdigest()


class DependencyInputModel(BaseModel):
    action: str = Field(description="The action (by hash) this dependency points to.")
    hash: str = Field(description="The hash of the action this dependency points to.")
    reference: str | None = Field(description="An optional reference used to identify this dependency within its graph.")
    binds: BindsInputModel | None = Field(description="The binds that constrain which implementations and clients may fulfill this dependency.")
    optional: bool = Field(default=False, description="Whether the dependency is optional or not.")
    viable_instances: int | None = Field(description="The number of viable instances required for this dependency.")


class WindowInputModel(BaseModel):
    window_function: enums.WindowFunction = Field(description="The aggregation to compute over the tracked value within the window.")
    label: str | None = Field(default=None, description="An optional human-readable label for the window.")


class TrackInputModel(BaseModel):
    dependency_key: str | None = Field(default=None, description="The key of the dependency whose state is being tracked.")
    state_key: str = Field(description="The key of the state to track.")
    value_key: str = Field(description="The key of the value within the state to track.")
    label: str | None = Field(default=None, description="An optional human-readable label for the track.")
    description: str | None = Field(default=None, description="An optional description for the track.")
    windows: list[WindowInputModel] | None = Field(default=None, description="The windows (aggregations) computed over the tracked value.")


class ImplementationInputModel(BaseModel):
    definition: DefinitionInputModel = Field(description="The definition of the implementation. This is used to uniquely identify the implementation")
    dependencies: list[AgentDependencyInputModel] = Field(default_factory=list, description="The agent dependencies required by this implementation.")
    tracks: list[TrackInputModel] | None = Field(default=None, description="The tracks of the definition. This is used to track values over time during the runtime of an action. This is the state of a dependency")
    interface: str = Field(description="The interface of the implementation. This is used to group implementations together in the UI")
    params: dict[str, Any] | None = Field(default=None, description="The params of the implementation. This is used to pass parameters to the implementation")
    instance_id: str | None = Field(default=None, description="The instance id of the agent this implementation is bound to.")
    locks: list[str] | None = Field(default=None, description="The locks of the implementation. This is used to specify which resources the implementation needs to run")
    optimistics: list[OptimisticInputModel] | None = Field(default=None, description="The optimistics of the definition. This is used to optimistically set state values when the action is assigned, to provide a better user experience.")
    manipulates: list[str] | None = Field(default=None, description="The states that the implementation manipulates. This is used to identify which states are manipulated by the implementation, and can be use to enhance state safety in the system")
    needs_token: bool = Field(default=True, description="Whether Rekuest should mint a signed provenance token when this implementation is assigned. Default true (provenance-by-default); set false for trivial/internal tasks that never produce external provenance.")
    provenance_audience: list[str] | None = Field(default=None, description="The downstream service(s) the provenance token should be scoped to (the token's `aud`). If omitted, Rekuest derives the audience from the structures the assignment acts on.")
    effects: enums.Effects = Field(default=enums.Effects.UNKNOWN, description="What running this implementation again would do to the world. Informational: shown to whoever decides about a lost task.")
    execution: enums.Execution = Field(default=enums.Execution.PLAIN, description="How this implementation runs: a WORKFLOW may call other actions and is resumed from its journal when its agent dies.")
    code_hash: str | None = Field(default=None, description="A hash of the implementation's code. A workflow is only resumed by an implementation with the same hash.")
    logo: str | None = Field(default=None, description="The key of the agent's action logo blok (context ACTION_LOGO) that draws this action.")
    asks: list["AskBlokInputModel"] | None = Field(default=None, description="The bloks this implementation asks a person through, declared up front: a task asks one by its key.")
    informs: list["InformBlokInputModel"] | None = Field(default=None, description="The bloks this implementation shows a person while a task goes on, declared up front: a task shows one by its key.")
    dynamic_asks: bool | None = Field(default=None, description="Whether a task may also ask through a blok made at ask time, or any blok of the organization. Refused otherwise, and without the `rekuest_dynamic_ask` scope.")
    dynamic_calls: bool | None = Field(default=None, description="Whether a task may also call actions it found at run time, not only its declared dependencies. A WORKFLOW only; refused otherwise, and without the `rekuest_dynamic_call` scope.")
    dynamic_informs: bool | None = Field(default=None, description="Whether a task may show a blok made at inform time, or any blok of the organization. Refused otherwise, and without the `rekuest_dynamic_inform` scope.")

    @model_validator(mode="after")
    def check_declared_interactions(self) -> Self:
        """Each ask and inform keyed once, across both; only a workflow calls actions it found at run time."""
        keys = [ask.key for ask in self.asks or []] + [inform.key for inform in self.informs or []]
        twice = next((key for key in keys if keys.count(key) > 1), None)
        if twice is not None:
            raise ValueError(f"Implementation {self.interface!r} declares the ask or inform {twice!r} twice")
        if self.dynamic_calls and self.execution != enums.Execution.WORKFLOW:
            raise ValueError(f"Implementation {self.interface!r} declares dynamic_calls, which only a WORKFLOW may")
        return self

    @model_validator(mode="after")
    def check_widget_targets(self) -> Self:
        """PROXY and STATE_CHOICE widgets may only name agent dependencies this implementation declares."""
        dependencies = {dependency.key: dependency for dependency in self.dependencies}

        def walk(ports: list[ArgPortInputModel], prefix: str = "") -> None:
            for port in ports:
                path = f"{prefix}{port.key}"
                for widget in iter_widget_chain(port.widget):
                    if widget.kind == "STATE_CHOICE" and widget.dependency is not None and widget.dependency not in dependencies:
                        raise ValueError(f"Widget STATE_CHOICE in port {path} names undeclared agent dependency {widget.dependency!r}")
                    if widget.kind == "PROXY" and widget.target_dependency is not None:
                        dependency = dependencies.get(widget.target_dependency)
                        if dependency is None:
                            raise ValueError(f"Widget PROXY in port {path} names undeclared agent dependency {widget.target_dependency!r}")
                        actions = {action.key for action in dependency.action_dependencies or []}
                        if actions and widget.target_action not in actions:
                            raise ValueError(f"Widget PROXY in port {path} targets action {widget.target_action!r}, which dependency {widget.target_dependency!r} does not declare (it declares {sorted(actions)})")
                walk(port.children or [], f"{path}{PORT_PATH_SEPARATOR}")

        walk(self.definition.args)
        return self

    @model_validator(mode="after")
    def check_action_bloks(self) -> Self:
        """The action's bloks name devices by its dependency keys: the ones this implementation declares."""
        arg_keys = [port.key for port in self.definition.args]
        for blok in self.definition.bloks:
            check_action_blok(blok, list(self.dependencies), arg_keys)
        return self


class StateDefinitionInputModel(BaseModel):
    ports: list[ReturnPortInputModel] = Field(description="The ports of the state schema. This is used to define the structure of the state")
    name: str = Field(description="The name of the state schema. This is used to uniquely identify the state schema")


class StateImplementationInputModel(BaseModel):
    interface: str = Field(description="The key of the state implementation. This is used to uniquely identify the state implementation")
    key: str | None = Field(default=None, description="The stable identity key of the state, matched by state demands. Defaults to the interface when omitted.")
    app: str | None = Field(default=None, description="The identifier of the app providing this state. Defaults to the registering agent's app identifier when omitted.")
    definition: StateDefinitionInputModel = Field(description="The schema of the state implementation. This is used to define the structure of the state")


class LockDefinitionInputModel(BaseModel):
    key: str = Field(description="The key of the lock. This is used to uniquely identify the lock")
    description: str | None = Field(default=None, description="Describe the lock a bit")


class LockImplementationInputModel(BaseModel):
    key: str = Field(description="The key of the lock implementation.")
    definition: LockDefinitionInputModel = Field(description="The lock definition this implementation fulfills.")


ASSET_SOURCE_MAX_LENGTH = 2000


class AssetInputModel(BaseModel):
    """A file an agent brings with it: the name its bloks call it by, and what the bytes are."""

    name: str = Field(description="The name bloks refer to the asset by, as in `@self.assets.<name>`. Unique within the agent.")
    kind: enums.AssetKind = Field(description="What the file is.")
    ref: str = Field(description="'sha256:' and the hex SHA-256 of the file's exact bytes.")
    size: int | None = Field(default=None, description="The size of the file in bytes. Required unless the asset names a `source`: an agent knows the size of a file it holds.")
    source: str | None = Field(default=None, description="The https URL the file can be fetched from. Given instead of holding the file: the agent uploads nothing, an admin's client fetches it.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _well_formed(self) -> "AssetInputModel":
        if not self.name.isidentifier():
            raise ValueError(f"Asset: the name {self.name!r} is not an identifier")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.ref):
            raise ValueError(f"Asset {self.name!r}: the reference must be 'sha256:' and 64 lowercase hex characters, not {self.ref!r}")
        if self.source is not None and not (self.source.startswith("https://") and len("https://") < len(self.source) <= ASSET_SOURCE_MAX_LENGTH):
            raise ValueError(f"Asset {self.name!r}: a source is an https URL of at most {ASSET_SOURCE_MAX_LENGTH} characters")
        if self.size is None and self.source is None:
            raise ValueError(f"Asset {self.name!r}: an asset has a size")
        if self.size is not None and self.size <= 0:
            raise ValueError(f"Asset {self.name!r}: an asset has a size")
        return self


# A two-field `BlokImplementationInputModel` stub used to sit here. The real one is
# declared further down and shadowed it at import time, so the stub was never reachable
# -- and its `definition: LockDefinitionInputModel` named a lock, not a blok.


# ============================================================================
# Blok trees (blok language v2, ``rekuest_core/vocabulary``)
# ============================================================================
# A v2 blok is stored as one tree of ComponentNodeInput {id, component, props?, children?}.
# A prop holds at most one of static_value (any JSON) | dynamic_value {path} | util_call |
# agent_call (its ``into`` name in declares_value); a prop or argument that holds none is
# null. ``queries.x.refetch()`` / ``.fetch_more(...)`` are util_calls of the effects
# ``query.refetch`` / ``query.fetch_more``, the query named by a literal. Paths are written as
# in BSX (``deps.robot.arm.joints``, ``move.progress``).
# Port calls (effects, validators, widgets) share the call models but stay on base@1.


class DynamicValueInputModel(BaseModel):
    """A path a prop reads, as BSX writes it (``deps.robot.arm.joints``, ``event.value``, a bare local)."""

    literal: str | None = Field(default=None, description="A static fallback literal value (serialized string or JSON primitive) used when `path` does not resolve.")
    path: str | None = Field(default=None, description="The path the prop reads: a root (`self`, `deps`, `args`, `task`, `queries`, `subscriptions`, `viewer`, `then`, `event`, `error`), a local, a loop variable or a record, and what follows it.")


class AgentProbeInputModel(BaseModel):
    """A task call: ``self.<action>(...)`` or ``deps.<key>.<action>(...)``, a callback's one thing.

    Attributes:
        dependency: ``self`` or the dependency key of the blok.
        operation: The action, by its key in the dependency's demands.
        arguments: Its arguments, every one named.
    """

    dependency: str = Field(min_length=1, description="`self`, or the dependency key the task goes to (`deps.<key>`).")
    operation: str = Field(min_length=1, description="The action called, by its key in the dependency's action demands.")
    arguments: Optional[List["ActionArgumentInputModel"]] = Field(default=None, description="Its arguments, each named.")

    @model_validator(mode="after")
    def check_argument_keys(self) -> Self:
        """Call arguments are a map: every entry needs a unique key."""
        _check_keyed(self.arguments, f"arguments of agent call {self.operation}")
        return self


class UtilCallInputModel(BaseModel):
    operation: str = Field(min_length=1, description="The function called (`str.upper`), or in a callback the effect (`ui.copy`, `task.answer`).")
    arguments: Optional[List["ActionArgumentInputModel"]] = Field(default=None, description="Key-value arguments map compiled for the target utility call.")

    @model_validator(mode="after")
    def check_argument_keys(self) -> Self:
        """Call arguments are a map: every entry needs a unique key."""
        _check_keyed(self.arguments, f"arguments of {self.operation}")
        return self


class ActionArgumentInputModel(BaseModel):
    """An argument of a call: a literal, a path, a nested call, a list or an object; null when it holds none.

    Attributes:
        key: The argument's name (none in a list).
        value_literal: A JSON literal.
        value_path: A path it reads.
    """

    key: str | None = Field(default=None, description="The argument property name.")
    value_literal: JsonValue = Field(default=None, description="A JSON literal.")
    value_path: Optional[str] = Field(default=None, description="A path the argument reads, as a prop's dynamic_value does.")

    # Separated nested calls
    agent_call: Optional["AgentProbeInputModel"] = Field(default=None, description="Defines a nested agent call if this argument should trigger an agent interaction.")
    util_call: Optional["UtilCallInputModel"] = Field(default=None, description="A nested function call.")

    value_list: Optional[List["ActionArgumentInputModel"]] = Field(default=None, description="Defines a list of values if this argument should be an array.")
    value_dict: Optional[List["ActionArgumentInputModel"]] = Field(default=None, description="Defines a list of key-value pairs if this argument should be a dictionary.")

    BINDINGS: ClassVar[tuple[str, ...]] = ("value_literal", "value_path", "agent_call", "util_call", "value_list", "value_dict")

    @model_validator(mode="after")
    def check_at_most_one_binding(self) -> Self:
        """An argument is bound at most one way (none: null); list entries are unkeyed, dict entries uniquely keyed."""
        bound = [name for name in self.BINDINGS if getattr(self, name) is not None]
        if len(bound) > 1:
            raise ValueError(f"ActionArgument {self.key!r} must set at most one of {', '.join(self.BINDINGS)} (got {bound})")
        _check_keyed(self.value_dict, f"value_dict of argument {self.key!r}")
        for entry in self.value_list or []:
            if entry.key is not None:
                raise ValueError(f"value_list entries of argument {self.key!r} must not carry a key")
        return self


# ============================================================================
# 2. Abstract Component Property Bindings
# ============================================================================
class ComponentPropInputModel(BaseModel):
    """One prop of a component node: a literal, a path, a function call, a task call (a callback's
    one thing, named by ``declares_value``), or nothing (null).

    Custom port widgets reuse it for their props under base@1 rules (:func:`_check_widget_props`).
    """

    key: str = Field(description="The prop key name matching the target UI catalog constraint.")
    static_value: JsonValue = Field(default=None, description="A JSON literal: a string, number, bool, list or object.")
    dynamic_value: Optional[DynamicValueInputModel] = Field(default=None, description="A path the prop reads.")
    declares_value: Optional[str] = Field(default=None, description="The name a task call writes its record to (`... into move`), or the loop variable of a `foreach` (its `let`).")

    # Separated top-level callbacks
    agent_call: Optional["AgentProbeInputModel"] = Field(default=None, description="A task call: a callback's one thing.")
    util_call: Optional["UtilCallInputModel"] = Field(default=None, description="A function the prop's value is, or in a callback the effect it runs (ui.copy, task.answer, query.refetch, query.fetch_more).")

    BINDINGS: ClassVar[tuple[str, ...]] = ("static_value", "dynamic_value", "agent_call", "util_call")

    @model_validator(mode="after")
    def check_at_most_one_binding(self) -> Self:
        """A prop is bound at most one way; one bound no way is null."""
        bound = [name for name in self.BINDINGS if getattr(self, name) is not None]
        if len(bound) > 1:
            raise ValueError(f"ComponentProp {self.key!r} must set at most one of {', '.join(self.BINDINGS)} (got {bound})")
        return self


# 3. The Unified Abstract Component Node Input
# ============================================================================
class ComponentNodeInputModel(BaseModel):
    """A node of a blok tree.

    Attributes:
        id: An explicit id (``^[A-Za-z_][A-Za-z0-9_.:-]*$``) or one derived from its place (``Card[0]/Button[1]``).
        component: The catalog component (``Button``) or language component (``if``, ``foreach``).
        props: Its props.
        children: Its children.
    """

    id: str = Field(description="An explicit id, or one derived from the node's place (`Card[0]/Button[1]`); unique in the tree.")
    component: str = Field(description="The catalog component (`Button`) or language component (`if`, `foreach`, `suspense`).")
    props: list[ComponentPropInputModel] | None = Field(default=None, description="The collection of static values, state pointers, or action endpoints assigned to this component.")
    children: list["ComponentNodeInputModel"] | None = Field(default=None, description="Its children, in order.")


class BlokLocalInputModel(BaseModel):
    """A local: blok-level form state that feeds a call, written only through ``bind`` on controls.

    A local is a port, typed like an action's argument: its ``key`` is the name the tree reads it
    by (``{iterations}``, never a reserved root), its ``kind`` says what the controls bound to it
    must write and what a task call may pass it to, its ``default`` is the value it starts with.
    A default that does not fit is the checker's finding (``local.default``), not a refusal of
    the port.

    ``kind`` is a port kind or ``ANY`` (an untyped value). Left out (``None``; a null is left out
    too) it is inferred from the tree (``rekuest_core.vocabulary.infer``); nothing fills it in
    here, so a left-out kind stays distinct from a given ``ANY`` until the scope is resolved. Only
    the fields the author sent are set (``model_fields_set``): a null is dropped before
    validation, so a resolved type fills what the author left out and nothing they said.
    """

    key: str = Field(description="The local's name, read bare in the tree (`{iterations}`); never a reserved root.")
    label: str | None = Field(default=None, description="Its label.")
    kind: enums.LocalKind | None = Field(default=None, description="What it holds: a port kind (what the controls bound to it must write, `locals.bind_kinds`, and what a task call may pass it to, `locals.arg_kinds`) or ANY. Left out, it is inferred from the tree.")
    description: str | None = Field(default=None, description="What it is for.")
    identifier: str | None = Field(default=None, description="The structure (or model, enum) identifier of what it holds.")
    nullable: bool = Field(default=False, description="Whether it may hold null.")
    effects: list[EffectInputModel] | None = Field(default=None, description="Its effects.")
    choices: list[ChoiceInputModel] | None = Field(default=None, description="The values it takes (an ENUM's).")
    reference_unit: str | None = Field(default=None, description="For a QUANTITY: its reference unit.")
    proposed_units: list[str] | None = Field(default=None, description="For a QUANTITY: the units offered.")
    dimension: str | None = Field(default=None, description="For a QUANTITY: its dimensionality (derived).")
    children: list[ArgPortInputModel] | None = Field(default=None, description="Its child ports (the item of a LIST, the fields of a DICT or MODEL).")
    validators: list[ValidatorInputModel] | None = Field(default=None, description="Its validators.")
    default: JsonValue = Field(default=None, description="The value it starts with.")
    widget: Optional["AssignWidgetInputModel"] = Field(default=None, description="The widget a form of it uses.")
    requires: list[RequiresInputModel] | None = Field(default=None, description="Its descriptor requirements.")
    persist: bool = Field(default=False, description="Remember the last input on this device (never on a server).")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _null_is_left_out(cls, value: object) -> object:
        """A null, or a GraphQL field left out (``UNSET``), is a field left out."""
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if item is not None and not isinstance(item, UnsetType)}
        return value

    @field_validator("widget", mode="before")
    @classmethod
    def _plain_widget(cls, value: object) -> object:
        return _as_plain(value)

    @model_validator(mode="after")
    def check_port(self) -> Self:
        """A typed local is held to the arg port rules (its default is the checker's,
        ``local.default``); an ``ANY`` or left-out one only to its key."""
        _check_port_key(self.key, "Local")
        port = self.arg_port()
        if port is not None and port.dimension != self.dimension:
            self.dimension = port.dimension
        return self

    @model_serializer(mode="wrap")
    def _left_out_stays_out(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """A local whose kind is left out goes out with only the fields its author sent, so
        whoever resolves it next (takt, at registration) still fills what was left out."""
        dumped: dict[str, object] = handler(self)
        if self.kind is not None:
            return dumped
        return {key: value for key, value in dumped.items() if key in self.model_fields_set}

    def arg_port(self) -> ArgPortInputModel | None:
        """The local as the arg port it is (its default left out), or None for an ``ANY`` or
        left-out kind: no port is ``ANY``."""
        if self.kind is None or self.kind == enums.LocalKind.ANY:
            return None
        fields = {name: getattr(self, name) for name in self.model_fields_set if name not in ("persist", "default", "kind")}
        return ArgPortInputModel.model_validate({**fields, "kind": enums.PortKind(self.kind.value)})


class BlokQueryInputModel(BaseModel):
    """A declared read: a GraphQL query on a service, re-run when its variables change."""

    key: str = Field(min_length=1, description="Its name: the blok reads it as `queries.<key>` (status, value, error, refetching, fetching_more).")
    service: str = Field(min_length=1, description="The service it runs on, by its key ('mikro').")
    document: str = Field(min_length=1, description="One GraphQL `query` operation; never a mutation.")
    variables: list[ActionArgumentInputModel] = Field(default_factory=list, description="Its variables, each an expression over args, locals and dependencies (as a call argument is stored).")
    poll: float | None = Field(default=None, gt=0, description="Re-run every this many seconds, besides when its variables change.")
    more: str | None = Field(default=None, description="The list field of its result that `queries.<key>.fetch_more(...)` appends new pages to, as a dotted path into the data (`positions`, `images.items`). A query without it cannot fetch more.")

    model_config = ConfigDict(extra="forbid")


class BlokSubscriptionInputModel(BaseModel):
    """A declared stream: a GraphQL subscription on a service, re-subscribed when its variables change."""

    key: str = Field(min_length=1, description="Its name: the blok reads it as `subscriptions.<key>` (status, value, error).")
    service: str = Field(min_length=1, description="The service it runs on, by its key.")
    document: str = Field(min_length=1, description="One GraphQL `subscription` operation.")
    variables: list[ActionArgumentInputModel] = Field(default_factory=list, description="Its variables, as a query's.")
    keep: int | None = Field(default=None, ge=1, description="Keep the last this many payloads as a list; the latest only when absent.")
    history: str | None = Field(default=None, description="A declared query to replay from while travelling; without it the value at travel start shows, stale.")

    model_config = ConfigDict(extra="forbid")


class BlokDemoInputModel(BaseModel):
    """What a preview renders the blok with, mirroring the roots it reads. Never what it shows live."""

    deps: JsonValue = Field(default=None, description="Per dependency key (`self` included), its states and tasks: `{self: {arm: {joints: [...]}}}`.")
    args: JsonValue = Field(default=None, description="What it is opened on.")
    task: JsonValue = Field(default=None, description="The task record it is about (task and action bloks, action logos).")
    queries: JsonValue = Field(default=None, description="Per declared query, the value it answers.")
    subscriptions: JsonValue = Field(default=None, description="Per declared subscription, its value.")

    model_config = ConfigDict(extra="forbid")


def check_blok_sent_once(components: object | None, source: str | None, *, required: bool) -> None:
    """A blok is sent as its components or as its BSX source: not both, and (where it must be sent) not neither."""
    from rekuest_core.vocabulary import BlokError

    if components is not None and source is not None:
        raise BlokError("manifest.sent", problem="not both")
    if required and components is None and source is None:
        raise BlokError("manifest.sent", problem="and this one has neither")


_CATALOG = re.compile(r"[a-z][a-z0-9_-]*@[0-9]+")
CATALOG_PATTERN = _CATALOG
"""How a catalog is spelled: ``name@version`` (``ui@1``)."""
_FIELD_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
"""A query's ``more``: a dotted path into its data (``positions``, ``images.items``)."""
_LOCAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GRAPHQL_COMMENT = re.compile(r"#[^\n]*")


def operation_keyword(document: str) -> str:
    """The kind of a GraphQL document's first operation: ``query`` (also for ``{ ... }``),
    ``mutation``, ``subscription``, or whatever word it starts with."""
    text = _GRAPHQL_COMMENT.sub("", document).lstrip()
    if text.startswith("{"):
        return "query"
    match = re.match(r"[A-Za-z_]+", text)
    return match.group(0) if match else ""


def check_operation(kind: str, name: str, operation: str) -> None:
    """A query's document is a ``query``, a subscription's a ``subscription``; neither writes."""
    from rekuest_core.vocabulary import BlokError

    if operation == kind:
        return
    if operation == "mutation":
        raise BlokError("query.read_only", kind=kind, name=name, problem="is a mutation: a blok reads, and writes only through a task")
    raise BlokError("query.kind", kind=kind, name=name, operation=operation or "document without an operation")


def wrong_context(kind: str, context: enums.ComponentContext) -> ValueError:
    """``context.kind``: a blok of ``kind`` drawn in a context its kind has no blok for."""
    from rekuest_core.vocabulary import BlokError, load_vocabulary

    contexts = load_vocabulary().kind(kind).contexts
    return BlokError("context.kind", n="n" if kind[:1] in "aeiou" else "", kind=kind, contexts=", ".join(contexts), context=context.value)


def check_blok_declarations(
    *,
    kind: str,
    catalogs: list[str],
    locals: list[BlokLocalInputModel],
    queries: list[BlokQueryInputModel],
    subscriptions: list[BlokSubscriptionInputModel],
    dependency_keys: set[str],
    demo: "BlokDemoInputModel | None",
) -> None:
    """What a v2 blok declares beside its tree, as arkitekt-spec's ``check_blok_declarations`` has it.

    Its catalogs spelled ``name@version``; its locals, queries and subscriptions each named once,
    by a name (a local by none the vocabulary reserves); a query a ``query``, a subscription a
    ``subscription``, neither a mutation; a query's ``more`` a dotted field path; a subscription's history a declared query; an action
    logo declaring nothing; ``demo.deps`` naming only dependencies (and ``self``). Each refusal
    is a row of the vocabulary's diagnostic table (``manifest.*``, ``query.*``, ``roots.reserved``).
    """
    from rekuest_core.vocabulary import BlokError, load_vocabulary

    reserved = load_vocabulary().reserved
    for catalog in catalogs:
        if not _CATALOG.fullmatch(catalog):
            raise BlokError("manifest.catalog", catalog=catalog)
    named: tuple[tuple[str, list[str]], ...] = (
        ("locals", [local.key for local in locals]),
        ("queries", [query.key for query in queries]),
        ("subscriptions", [subscription.key for subscription in subscriptions]),
    )
    for what, keys in named:
        twice = next((key for key in keys if keys.count(key) > 1), None)
        if twice is not None:
            raise BlokError("manifest.duplicate", what=what, key=twice)
        for key in keys:
            if not _LOCAL_NAME.fullmatch(key):
                raise BlokError("manifest.name", what=what, key=key)
            if what == "locals" and key in reserved:
                raise BlokError("roots.reserved", name=key)
    for query in queries:
        check_operation("query", query.key, operation_keyword(query.document))
        if query.more is not None and not _FIELD_PATH.fullmatch(query.more):
            raise BlokError("query.more", name=query.key, more=query.more, problem="is not a field path into its data (positions, images.items)")
    declared = {query.key for query in queries}
    for subscription in subscriptions:
        check_operation("subscription", subscription.key, operation_keyword(subscription.document))
        if subscription.history is not None and subscription.history not in declared:
            raise BlokError("query.history", name=subscription.key, history=subscription.history)
    if kind == "action_logo" and (locals or queries or subscriptions or dependency_keys):
        raise BlokError("manifest.action_logo")
    deps = demo.deps if demo is not None else None
    if isinstance(deps, dict):
        unknown = sorted(set(deps) - dependency_keys - {"self"})
        if unknown:
            raise BlokError("manifest.demo", keys=", ".join(unknown))


def blok_kind(context: enums.ComponentContext) -> str:
    """The kind of a blok an agent declares: an action logo when drawn as one, else an agent blok."""
    return "action_logo" if context == enums.ComponentContext.ACTION_LOGO else "agent"


def check_blok_manifest(
    components: Optional[List[ComponentNodeInputModel]],
    *,
    kind: str,
    context: enums.ComponentContext,
    dependencies: list[AgentDependencyInputModel],
    locals: list[BlokLocalInputModel],
    queries: list[BlokQueryInputModel],
    subscriptions: list[BlokSubscriptionInputModel],
    args: list[str] | None,
    catalogs: list[str],
    demo: "BlokDemoInputModel | None" = None,
) -> list["Finding"]:
    """A v2 tree against what its blok declares and its kind, without a catalog (the language
    rules of ``rekuest_core.vocabulary.check``): refused on the first error, its warnings returned.

    A tree is one root. Its catalog findings (component names, props, placement) are the
    catalog check's (``facade/catalog_validation.py``), which runs where the catalogs are.

    Raises:
        BlokError: With the code of the first error finding.
    """
    from rekuest_core.vocabulary import BlokError, BlokScope, check_blok_tree

    if components is None:
        return []
    if len(components) != 1:
        raise ValueError(f"Blok manifest: a blok has one root, not {len(components)}")
    # A tree sent as such was never parsed: its ids are held to what the parser holds them to.
    seen: set[str] = set()
    for node in iter_component_nodes(components):
        if node.id in seen:
            raise BlokError("bsx.duplicate_id", id=node.id)
        seen.add(node.id)
    scope = BlokScope(
        kind=kind,
        context=context.value,
        dependencies=tuple(dependencies),
        locals=tuple(locals),
        queries=tuple(queries),
        subscriptions=tuple(subscriptions),
        args=tuple(args) if args is not None else None,
        catalogs=tuple(catalogs),
        demo=demo,
    )
    findings = check_blok_tree(components[0], scope, None)
    for finding in findings:
        if finding.level == "error":
            raise BlokError.from_finding(finding)
    return findings


MODEL_REF_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def check_blok_target(context: enums.ComponentContext, for_model: Optional[str], for_node: Optional[str]) -> None:
    """A blok that decorates a model names it by reference, and is an html blok."""
    if for_node is not None and for_model is None:
        raise ValueError("Blok manifest: for_node needs a for_model")
    if for_model is None:
        return
    if not MODEL_REF_PATTERN.fullmatch(for_model):
        raise ValueError(f"Blok manifest: for_model must be 'sha256:' and 64 lowercase hex characters, not {for_model!r}")
    if context != enums.ComponentContext.HTML:
        raise ValueError("Blok manifest: only an html blok can decorate a model")


BLOK_ARGS_ROOT = "args"


def check_blok_args(context: enums.ComponentContext, args: list["ArgPortInputModel"]) -> None:
    """A blok's args are the items it is opened for: an html blok's, keyed once, read under ``args.<key>``."""
    if not args:
        return
    if context != enums.ComponentContext.HTML:
        raise ValueError(f"Blok manifest: only an html blok takes args, not a {context.value} blok")
    _check_unique(args, "key", "Blok manifest args")


class BlokImplementationInputModel(BaseModel):
    "A blok an agent declares (blok language v2): its tree (or BSX source) and what it declares beside it."

    key: str = Field(description="The key of this Blok implementation.")
    dependencies: list[AgentDependencyInputModel] = Field(default_factory=list, description="The dependencies required by this Blok: for an agent's blok the one dependency `self`, its demands what the tree reads and calls of it.")
    components: Optional[list[ComponentNodeInputModel]] = Field(default=None, description="The UI component tree blueprint for this Blok (one root), unless it is sent as its `source`.")
    source: Optional[str] = Field(default=None, description="The Blok as v2 BSX, parsed by the server into its component tree. Exactly one of `components` and `source` is sent.")
    module: Optional[str] = Field(default=None, description="The module (an app's own unversioned extras, registered by a UI app such as the Orkestrator) the blok renders against besides its catalogs; the Orkestrator's when absent.")
    description: Optional[str] = Field(default=None, description="A human-readable description about this Blok's purpose and functionality.")
    context: enums.ComponentContext = Field(default=enums.ComponentContext.HTML, description="The renderer for this Blok's root: HTML for panels and dashboards, SCENE for a full 3D view whose root is a Scene, LOGO an agent's logo, ACTION_LOGO an action's.")
    default: bool = Field(default=False, description="Whether this Blok is its agent's default for its context. A newly declared default replaces the previous one.")
    for_model: Optional[str] = Field(default=None, description="The model ('sha256:...') this Blok decorates: viewers showing that model offer this Blok at `for_node`. HTML bloks only.")
    for_node: Optional[str] = Field(default=None, description="The node of `for_model` this Blok attaches to; the model's root when absent.")
    args: list[ArgPortInputModel] = Field(default_factory=list, description="The items this Blok is opened for, declared like an action's args and read under `args.<key>`. A UI offers the Blok on any item its args accept (`MaterializedBlokFilter.demands`). HTML bloks only.")
    vocab: Literal[2] = Field(description="The blok language version the blok speaks; 2 is the only one.")
    catalogs: list[str] = Field(default_factory=list, description="The catalogs it needs, as name@version ('ui@1', 'scene@1').")
    locals: list[BlokLocalInputModel] = Field(default_factory=list, description="Its locals: form state that feeds a call, written only through bind.")
    demo: Optional[BlokDemoInputModel] = Field(default=None, description="What a preview renders it with.")
    queries: list[BlokQueryInputModel] = Field(default_factory=list, description="Its declared reads, as `queries.<key>`.")
    subscriptions: list[BlokSubscriptionInputModel] = Field(default_factory=list, description="Its declared streams, as `subscriptions.<key>`.")

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        """Sent one way; its declarations well-formed; its tree (when sent as one) coherent with them.

        A blok sent as its source is checked once takt has parsed it, at registration."""
        check_blok_sent_once(self.components, self.source, required=True)
        if self.context == enums.ComponentContext.OVERLAY:
            raise wrong_context("agent", self.context)
        kind = blok_kind(self.context)
        dependency_keys = {dependency.key for dependency in self.dependencies}
        check_blok_declarations(kind=kind, catalogs=self.catalogs, locals=self.locals, queries=self.queries, subscriptions=self.subscriptions, dependency_keys=dependency_keys, demo=self.demo)
        check_blok_target(self.context, self.for_model, self.for_node)
        check_blok_args(self.context, self.args)
        check_blok_manifest(
            self.components,
            kind=kind,
            context=self.context,
            dependencies=self.dependencies,
            locals=self.locals,
            queries=self.queries,
            subscriptions=self.subscriptions,
            args=[arg.key for arg in self.args] if self.args else None,
            catalogs=self.catalogs,
            demo=self.demo,
        )
        return self


#: The contexts an action has a blok for, one each.
ACTION_BLOK_CONTEXTS = (enums.ComponentContext.HTML, enums.ComponentContext.SCENE, enums.ComponentContext.OVERLAY)

#: The component that anchors its children at the agent bound to a dependency (``dep``).
ANCHOR_COMPONENT = "Anchor"

#: The component that draws a line through the agents bound to dependencies (``through``).
ROUTE_COMPONENT = "Route"

#: The run's open ask form.
ASK_COMPONENT = "ask"

#: The components that read a run or a dependency of one: only an action's own blok (or an overlay) holds them.
ACTION_BLOK_COMPONENTS = (ANCHOR_COMPONENT, ROUTE_COMPONENT, ASK_COMPONENT)


def anchored_keys(components: Optional[List[ComponentNodeInputModel]]) -> list[tuple[str, str]]:
    """Every dependency key an ``<Anchor dep>`` or ``<Route through>`` names, with the node's id."""
    named: list[tuple[str, str]] = []
    for node in iter_component_nodes(components):
        for prop in node.props or []:
            if node.component == ANCHOR_COMPONENT and prop.key == "dep" and isinstance(prop.static_value, str):
                named.append((prop.static_value, node.id))
            elif node.component == ROUTE_COMPONENT and prop.key == "through" and isinstance(prop.static_value, list):
                named.extend((key, node.id) for key in prop.static_value if isinstance(key, str))
    return named


def check_action_blok_anchors(components: Optional[List[ComponentNodeInputModel]], dependency_keys: set[str]) -> None:
    """Every ``<Anchor dep>`` and ``<Route through>`` names dependencies of the action. Bound
    dynamically, they are left to the renderer."""
    for key, node_id in anchored_keys(components):
        if key not in dependency_keys:
            known = ", ".join(repr(known) for known in sorted(dependency_keys)) or "none"
            raise ValueError(f"Blok manifest: component {node_id!r} names {key!r}, which is not a dependency of the action (its dependencies: {known})")


def check_action_blok(blok: "ActionBlokInputModel", dependencies: list[AgentDependencyInputModel], arg_keys: list[str] | None = None) -> list["Finding"]:
    """An action's blok against the dependencies its implementation declares (kind ``action``).

    It reads the run as ``task``, its args as ``args``, the agent running it as ``self`` and the
    agents the run got as ``deps.<key>``; ``Anchor``/``Route`` name only the dependencies. A
    blok sent as its source is checked once takt has parsed it. Its warnings are returned.

    Raises:
        BlokError: With the code of the first error finding.
    """
    keys = {dependency.key for dependency in dependencies}
    check_blok_declarations(kind="action", catalogs=blok.catalogs, locals=blok.locals, queries=blok.queries, subscriptions=blok.subscriptions, dependency_keys=keys, demo=blok.demo)
    if blok.components is None:
        return []
    check_action_blok_anchors(blok.components, keys)
    return check_blok_manifest(
        blok.components,
        kind="action",
        context=blok.context,
        dependencies=dependencies,
        locals=blok.locals,
        queries=blok.queries,
        subscriptions=blok.subscriptions,
        args=arg_keys,
        catalogs=blok.catalogs,
        demo=blok.demo,
    )


class ActionBlokInputModel(BaseModel):
    """An action's own blok: how a client shows a run of it in one context. It has no name and is never listed
    on its own; its dependencies are the action's, which it names by key."""

    context: enums.ComponentContext = Field(default=enums.ComponentContext.HTML, description="Where a client shows the run with it: HTML in a task panel, SCENE as a 3D view of its own, OVERLAY drawn into the space that hosts the run (its root an `Overlay`).")
    components: Optional[list[ComponentNodeInputModel]] = Field(default=None, description="The UI component tree, unless the blok is sent as its `source`.")
    source: Optional[str] = Field(default=None, description="The blok as v2 BSX, parsed by the server into its component tree. Exactly one of `components` and `source` is sent.")
    module: Optional[str] = Field(default=None, description="The module the blok renders against besides its catalogs; the Orkestrator's when absent.")
    description: Optional[str] = Field(default=None, description="What the blok shows of a run.")
    vocab: Literal[2] = Field(description="The blok language version the blok speaks; 2 is the only one.")
    catalogs: list[str] = Field(default_factory=list, description="The catalogs it needs, as name@version.")
    locals: list[BlokLocalInputModel] = Field(default_factory=list, description="Its locals: form state that feeds a call, written only through bind.")
    demo: Optional[BlokDemoInputModel] = Field(default=None, description="What a preview renders it with.")
    queries: list[BlokQueryInputModel] = Field(default_factory=list, description="Its declared reads.")
    subscriptions: list[BlokSubscriptionInputModel] = Field(default_factory=list, description="Its declared streams.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_action_blok(self) -> Self:
        """Sent one way, in a context an action has a blok for."""
        check_blok_sent_once(self.components, self.source, required=True)
        if self.context not in ACTION_BLOK_CONTEXTS:
            raise wrong_context("action", self.context)
        return self


class AskBlokInputModel(BaseModel):
    """A blok an action asks a person through, declared with it: made once at registration, and
    the only kind of ask a task may hold on unless its implementation declares `dynamic_asks`. A
    task blok: it reads the run as `task`, its args as `args.<key>`, the agent as `self`, and
    answers with `task.answer(values: {...})`, keyed by its returns."""

    key: str = Field(description="What a task names it by: `<parameter>.<method>` of the action's interactions protocol.")
    description: Optional[str] = Field(default=None, description="What it asks, in a sentence: the hold's message wherever the ask is listed.")
    components: Optional[list[ComponentNodeInputModel]] = Field(default=None, description="The UI component tree, unless the blok is sent as its `source`.")
    source: Optional[str] = Field(default=None, description="The blok as v2 BSX, parsed by the server into its component tree. Exactly one of `components` and `source` is sent.")
    module: Optional[str] = Field(default=None, description="The module the blok renders against besides its catalogs; the Orkestrator's when absent.")
    args: list[ArgPortInputModel] = Field(default_factory=list, description="What it is opened on, read under `args.<key>`.")
    returns: list[ReturnPortInputModel] = Field(default_factory=list, description="What an answer holds.")
    dependencies: list[AgentDependencyInputModel] = Field(default_factory=list, description="The agents it reads or calls besides `self`, as `deps.<key>`.")
    vocab: Literal[2] = Field(description="The blok language version the blok speaks; 2 is the only one.")
    catalogs: list[str] = Field(default_factory=list, description="The catalogs it needs, as name@version.")
    locals: list[BlokLocalInputModel] = Field(default_factory=list, description="Its locals: form state that feeds a call, written only through bind.")
    demo: Optional[BlokDemoInputModel] = Field(default=None, description="What a preview renders it with.")
    queries: list[BlokQueryInputModel] = Field(default_factory=list, description="Its declared reads.")
    subscriptions: list[BlokSubscriptionInputModel] = Field(default_factory=list, description="Its declared streams.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_ask_blok(self) -> Self:
        """Sent one way; its declarations a task blok's; its args and returns each keyed once."""
        check_blok_sent_once(self.components, self.source, required=True)
        check_blok_declarations(
            kind="task",
            catalogs=self.catalogs,
            locals=self.locals,
            queries=self.queries,
            subscriptions=self.subscriptions,
            dependency_keys={dependency.key for dependency in self.dependencies} | {"self"},
            demo=self.demo,
        )
        for what, ports in (("args", self.args), ("returns", self.returns)):
            keys = [port.key for port in ports]
            twice = next((key for key in keys if keys.count(key) > 1), None)
            if twice is not None:
                raise ValueError(f"Ask {self.key!r}: duplicate {what} key {twice!r}")
        return self


class InformBlokInputModel(BaseModel):
    """A blok an action shows a person while its task goes on, declared with it: made once at
    registration, and the only kind of inform a task may show unless its implementation
    declares `dynamic_informs`. A task blok: it reads the run as `task`, its args as
    `args.<key>` and the agent as `self`; nothing waits on it, so it has no returns."""

    key: str = Field(description="What a task names it by: `<parameter>.<method>` of the action's interactions protocol.")
    description: Optional[str] = Field(default=None, description="What it shows, in a sentence: the inform's message wherever it is listed.")
    components: Optional[list[ComponentNodeInputModel]] = Field(default=None, description="The UI component tree, unless the blok is sent as its `source`.")
    source: Optional[str] = Field(default=None, description="The blok as v2 BSX, parsed by the server into its component tree. Exactly one of `components` and `source` is sent.")
    module: Optional[str] = Field(default=None, description="The module the blok renders against besides its catalogs; the Orkestrator's when absent.")
    args: list[ArgPortInputModel] = Field(default_factory=list, description="What it is opened on, read under `args.<key>`.")
    dependencies: list[AgentDependencyInputModel] = Field(default_factory=list, description="The agents it reads or calls besides `self`, as `deps.<key>`.")
    vocab: Literal[2] = Field(description="The blok language version the blok speaks; 2 is the only one.")
    catalogs: list[str] = Field(default_factory=list, description="The catalogs it needs, as name@version.")
    locals: list[BlokLocalInputModel] = Field(default_factory=list, description="Its locals: form state that feeds a call, written only through bind.")
    demo: Optional[BlokDemoInputModel] = Field(default=None, description="What a preview renders it with.")
    queries: list[BlokQueryInputModel] = Field(default_factory=list, description="Its declared reads.")
    subscriptions: list[BlokSubscriptionInputModel] = Field(default_factory=list, description="Its declared streams.")

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_inform_blok(self) -> Self:
        """Sent one way; its declarations a task blok's; its args each keyed once."""
        check_blok_sent_once(self.components, self.source, required=True)
        check_blok_declarations(
            kind="task",
            catalogs=self.catalogs,
            locals=self.locals,
            queries=self.queries,
            subscriptions=self.subscriptions,
            dependency_keys={dependency.key for dependency in self.dependencies} | {"self"},
            demo=self.demo,
        )
        keys = [port.key for port in self.args]
        twice = next((key for key in keys if keys.count(key) > 1), None)
        if twice is not None:
            raise ValueError(f"Inform {self.key!r}: duplicate args key {twice!r}")
        return self


class WidgetDefaultInputModel(BaseModel):
    """A catalog's default widget for ports matching a kind and/or structure identifier."""

    kind: enums.PortKind | None = Field(default=None, description="Port kind the default applies to. With `identifier`, both must match.")
    identifier: str | None = Field(default=None, description="Structure identifier the default applies to, e.g. '@mikro/image'.")
    widget: Optional["AssignWidgetInputModel"] = Field(default=None, description="The assign widget to render for matching argument ports that declare no widget of their own.")
    return_widget: Optional["ReturnWidgetInputModel"] = Field(default=None, description="The return widget to render for matching return ports that declare no widget of their own.")

    @field_validator("widget", "return_widget", mode="before")
    @classmethod
    def _plain_widgets(cls, value: Any) -> Any:
        return _as_plain(value)

    @model_validator(mode="after")
    def check_selector_and_widget(self) -> Self:
        """A default needs something to match on and something to render."""
        if self.kind is None and self.identifier is None:
            raise ValueError("WidgetDefault needs a kind and/or an identifier to match ports on")
        if self.widget is None and self.return_widget is None:
            raise ValueError("WidgetDefault needs a widget and/or a return_widget")
        return self

    @property
    def selector(self) -> tuple[str | None, str | None]:
        return (self.kind.value if self.kind is not None else None, self.identifier)


SearchAssignWidgetInputModel.model_rebuild()
CustomAssignWidgetInputModel.model_rebuild()
StateChoiceAssignWidgetInputModel.model_rebuild()
CustomReturnWidgetInputModel.model_rebuild()
ArgPortInputModel.model_rebuild()
BlokLocalInputModel.model_rebuild()
ReturnPortInputModel.model_rebuild()
WidgetDefaultInputModel.model_rebuild()
StateAccessorInputModel.model_rebuild()
OptimisticInputModel.model_rebuild()
DefinitionInputModel.model_rebuild()
ImplementationInputModel.model_rebuild()


# ============================================================================
# UI catalog registry: what a UI app can render (components) and evaluate (operations)
# ============================================================================
class CatalogEventFieldInputModel(BaseModel):
    key: str = Field(min_length=1, description="The field a callback reads as `event.<key>`.")
    kind: enums.CatalogValueKind = Field(description="The value kind of the field.")
    description: str | None = Field(default=None, description="Human-readable description of the field.")


class CatalogPropInputModel(BaseModel):
    key: str = Field(min_length=1, description="The prop key a ComponentProp.key must match.")
    kind: enums.CatalogValueKind = Field(description="The value kind this prop accepts. CALLBACK props hold one task call or one effect. On `bind`, the kind the control writes (`locals.bind_kinds`).")
    required: bool = Field(default=False, description="Whether every component instance must set this prop.")
    description: str | None = Field(default=None, description="Human-readable description of the prop.")
    event: list[CatalogEventFieldInputModel] | None = Field(default=None, description="For a CALLBACK prop: the fields of the event it hands its callback (`event.<key>`); none when it hands none.")


class CatalogComponentInputModel(BaseModel):
    name: str = Field(min_length=1, description="The component name a ComponentNode.component (or a custom widget's component) must match.")
    description: str | None = Field(default=None, description="Human-readable description of the component.")
    props: list[CatalogPropInputModel] = Field(default_factory=list, description="The props this component accepts.")
    accepts_children: bool = Field(default=True, description="Whether ComponentNode.children may be nested under this component.")
    context: enums.ComponentContext = Field(default=enums.ComponentContext.HTML, description="The context this component may be placed in.")
    children_context: enums.ComponentContext | None = Field(default=None, description="The context in force for this component's children, when it differs from its own: such a component bridges two renderers.")
    inherits_context: bool = Field(default=False, description="Whether the component may be placed in any context, taking its parent's for itself and its children (control flow such as foreach or if). Its context is then ignored.")
    downstream_of: list[str] = Field(default_factory=list, description="Component names of which at least one must be an ancestor, at any depth. Empty means no requirement.")
    not_downstream_of: list[str] = Field(default_factory=list, description="Component names of which none may be an ancestor.")
    text_prop: str | None = Field(default=None, description="The prop text children fill (`<Button>Go</Button>` sets `label`); none: the component takes no text.")
    catalog: str | None = Field(default=None, description="The catalog it belongs to, as name@version (`ui@1`), or the name of the module it is from.")

    @model_validator(mode="after")
    def check_unique_props(self) -> Self:
        """Prop keys are unique per component."""
        _check_unique(self.props, "key", f"catalog component {self.name}")
        return self

    @model_validator(mode="after")
    def check_no_action_logo_context(self) -> Self:
        """ACTION_LOGO and OVERLAY are a blok's contexts, never a component's: an action logo draws logo
        components, an overlay scene components."""
        if enums.ComponentContext.ACTION_LOGO in (self.context, self.children_context):
            raise ValueError(f"catalog component {self.name}: ACTION_LOGO is a blok context only; its shapes are LOGO components")
        if enums.ComponentContext.OVERLAY in (self.context, self.children_context):
            raise ValueError(f"catalog component {self.name}: OVERLAY is a blok context only; what an overlay draws are SCENE components")
        return self


class CatalogArgumentInputModel(BaseModel):
    key: str = Field(min_length=1, description="The argument key a UtilCall argument must use.")
    kind: enums.CatalogValueKind = Field(description="The value kind of the argument.")
    required: bool = Field(default=True, description="Whether every call must pass this argument.")
    description: str | None = Field(default=None, description="Human-readable description of the argument.")


class CatalogOperationInputModel(BaseModel):
    name: str = Field(min_length=1, description="The operation name a UtilCall.operation must match.")
    description: str | None = Field(default=None, description="Human-readable description of the operation.")
    arguments: list[CatalogArgumentInputModel] = Field(default_factory=list, description="The arguments the operation accepts.")
    returns: enums.CatalogValueKind = Field(description="The kind of value the operation returns (BOOL for effect and validator calls).")

    @model_validator(mode="after")
    def check_unique_arguments(self) -> Self:
        """Argument keys are unique per operation."""
        _check_unique(self.arguments, "key", f"catalog operation {self.name}")
        return self
