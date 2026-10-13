"""Widgets as a discriminated union: per-kind strict inputs, port compatibility, and the remaining widget rules."""

import pytest
from pydantic import TypeAdapter, ValidationError

from rekuest_core.inputs import models as imodels
from rekuest_core.objects import models as omodels

ASSIGN = TypeAdapter(imodels.AssignWidgetInputModel)
RETURN = TypeAdapter(imodels.ReturnWidgetInputModel)

SEARCH_QUERY = "query Search($search: String, $values: [ID!]) { options: things(search: $search, ids: $values) { value: id label: name } }"


def _widget(kind: str, **fields: object) -> imodels._AssignWidgetBase:
    return ASSIGN.validate_python({"kind": kind, **fields})


def _port(key: str = "foo", kind: str = "STRING", **extra: object) -> imodels.ArgPortInputModel:
    return imodels.ArgPortInputModel(key=key, kind=kind, nullable=False, **extra)


@pytest.mark.parametrize(
    ("kind", "fields", "model"),
    [
        ("SLIDER", {"min": 0, "max": 10, "step": 1}, imodels.SliderAssignWidgetInputModel),
        ("SLIDER", {}, imodels.SliderAssignWidgetInputModel),
        ("CHOICE", {"placeholder": "pick"}, imodels.ChoiceAssignWidgetInputModel),
        ("STRING", {"placeholder": "type", "as_paragraph": True}, imodels.StringAssignWidgetInputModel),
        ("SEARCH", {"query": SEARCH_QUERY, "ward": "mikro", "dependencies": ["other"], "placeholder": "pick"}, imodels.SearchAssignWidgetInputModel),
        ("CUSTOM", {"component": "Knob", "dependencies": ["other"], "props": [{"key": "value", "dynamic_value": {"path": "/value"}}], "fallback": {"kind": "STRING"}}, imodels.CustomAssignWidgetInputModel),
        ("STATE_CHOICE", {"state_path": "/positions", "dependency": "stage", "state_accessors": [{"option_key": "LABEL", "path": "/name"}]}, imodels.StateChoiceAssignWidgetInputModel),
        ("STATE_CHOICE", {"state_call": {"operation": "pick", "arguments": [{"key": "s", "value_path": "/state/positions"}]}}, imodels.StateChoiceAssignWidgetInputModel),
        ("PROXY", {"target_port": "image", "target_action": "acquire", "target_dependency": "camera"}, imodels.ProxyAssignWidgetInputModel),
    ],
)
def test_kind_selects_the_member_model(kind: str, fields: dict, model: type) -> None:
    """Kind selects the member model."""
    assert isinstance(_widget(kind, **fields), model)


@pytest.mark.parametrize(
    ("kind", "fields", "message"),
    [
        ("SEARCH", {"ward": "mikro"}, "query"),
        ("SEARCH", {"query": SEARCH_QUERY, "ward": "w", "min": 1}, "Extra inputs are not permitted"),
        ("SLIDER", {"state_path": "/x"}, "Extra inputs are not permitted"),
        ("STRING", {"component": "Knob"}, "Extra inputs are not permitted"),
        ("CUSTOM", {}, "component"),
        ("STATE_CHOICE", {}, "exactly one of state_path or state_call"),
        ("STATE_CHOICE", {"state_path": "/x", "state_call": {"operation": "pick"}}, "exactly one of state_path or state_call"),
        ("PROXY", {"target_port": "image"}, "target_action"),
        ("KNOB", {}, "kind"),
    ],
)
def test_fields_of_another_kind_and_missing_fields_are_rejected(kind: str, fields: dict, message: str) -> None:
    """Fields of another kind and missing fields are rejected."""
    with pytest.raises(ValidationError, match=message):
        _widget(kind, **fields)


def test_slider_range_and_step_invariants() -> None:
    """Slider range and step invariants."""
    with pytest.raises(ValidationError, match=r"min \(5\.0\) must be smaller than max \(1\.0\)"):
        _widget("SLIDER", min=5, max=1)
    with pytest.raises(ValidationError, match="step must be positive"):
        _widget("SLIDER", step=0)
    with pytest.raises(ValidationError, match="default 20 lies outside the SLIDER range"):
        _port(kind="INT", default=20, widget={"kind": "SLIDER", "min": 0, "max": 10})
    _port(kind="INT", default=5, widget={"kind": "SLIDER", "min": 0, "max": 10})


def test_search_query_is_parsed_and_must_declare_its_variables() -> None:
    """Search query is parsed and must declare its variables."""
    with pytest.raises(ValidationError, match="does not parse"):
        _widget("SEARCH", query="query {", ward="mikro")
    with pytest.raises(ValidationError, match=r"must declare the variables \['\$values'\]"):
        _widget("SEARCH", query="query S($search: String) { x }", ward="mikro")
    with pytest.raises(ValidationError, match="exactly one `query` operation"):
        _widget("SEARCH", query="mutation M($search: String, $values: [ID!]) { x }", ward="mikro")
    with pytest.raises(ValidationError, match=r"missing \['\$stage'\]"):
        _widget("SEARCH", query=SEARCH_QUERY, ward="mikro", filters=[{"key": "stage", "kind": "STRING", "nullable": False}])
    with pytest.raises(ValidationError, match="'value' is a reserved port key"):
        _widget("SEARCH", query="query S($search: String, $values: [ID!], $value: ID) { x }", ward="mikro", filters=[{"key": "value", "kind": "STRING", "nullable": False}])
    widget = _widget("SEARCH", query="query S($search: String, $values: [ID!], $stage: ID) { x }", ward="mikro", filters=[{"key": "stage", "kind": "STRING", "nullable": False}])
    assert widget.filters[0].key == "stage"


def test_widget_kind_must_fit_the_port_kind() -> None:
    """Widget kind must fit the port kind."""
    with pytest.raises(ValidationError, match="of kind STRING cannot use a SLIDER widget"):
        _port(kind="STRING", widget={"kind": "SLIDER"})
    with pytest.raises(ValidationError, match="of kind INT cannot use a STRING widget"):
        _port(kind="INT", widget={"kind": "STRING"})
    with pytest.raises(ValidationError, match="of kind STRING cannot use a SEARCH widget"):
        _port(kind="STRING", widget={"kind": "SEARCH", "query": SEARCH_QUERY, "ward": "mikro"})
    with pytest.raises(ValidationError, match="SEARCH widget on a LIST port needs a STRUCTURE child"):
        _port(kind="LIST", children=[_port("item", "STRING").model_dump()], widget={"kind": "SEARCH", "query": SEARCH_QUERY, "ward": "mikro"})
    _port(kind="LIST", children=[_port("item", "STRUCTURE", identifier="@mikro/image").model_dump()], widget={"kind": "SEARCH", "query": SEARCH_QUERY, "ward": "mikro"})
    _port(kind="QUANTITY", reference_unit="volt", widget={"kind": "SLIDER"})
    # fallbacks are held to the same rule
    with pytest.raises(ValidationError, match="of kind STRING cannot use a SLIDER widget"):
        _port(kind="STRING", widget={"kind": "CUSTOM", "component": "Knob", "fallback": {"kind": "SLIDER"}})


def test_choices_live_on_the_port() -> None:
    """A CHOICE widget renders the port's choices; a default must be one of them."""
    with pytest.raises(ValidationError, match="a CHOICE widget needs the port to declare `choices`"):
        _port(widget={"kind": "CHOICE"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _widget("CHOICE", choices=[{"value": "a", "label": "A"}])
    choices = [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]
    with pytest.raises(ValidationError, match=r"default 'c' is not one of its choices \['a', 'b'\]"):
        _port(choices=choices, default="c")
    port = _port(choices=choices, default="b", widget={"kind": "CHOICE", "placeholder": "pick"})
    out = omodels.ArgPortModel(**port.model_dump())
    assert isinstance(out.widget, omodels.ChoiceAssignWidgetModel) and out.widget.placeholder == "pick"
    assert [c.value for c in out.choices] == ["a", "b"]

    with pytest.raises(ValidationError, match="a CHOICE return widget needs the port to declare `choices`"):
        imodels.ReturnPortInputModel(key="out", kind="STRING", nullable=False, widget={"kind": "CHOICE"})
    ret = imodels.ReturnPortInputModel(key="out", kind="STRING", nullable=False, choices=choices, widget={"kind": "CHOICE"})
    assert isinstance(omodels.ReturnPortModel(**ret.model_dump()).widget, omodels.ChoiceReturnWidgetModel)


def test_custom_widget_props_must_be_pure_and_scoped() -> None:
    """Custom widget props must be pure and scoped."""
    with pytest.raises(ValidationError, match="agent calls are not allowed in widgets"):
        _widget("CUSTOM", component="Knob", props=[{"key": "onClick", "agent_call": {"dependency": "stage", "operation": "move"}}])
    with pytest.raises(ValidationError, match="references 'other' via dynamic_value.path"):
        _widget("CUSTOM", component="Knob", props=[{"key": "x", "dynamic_value": {"path": "/other/v"}}])
    with pytest.raises(ValidationError, match="references 'other' via value_path"):
        _widget("CUSTOM", component="Knob", props=[{"key": "x", "util_call": {"operation": "fmt", "arguments": [{"key": "v", "value_path": "/other/v"}]}}])
    _widget("CUSTOM", component="Knob", dependencies=["other"], props=[{"key": "x", "util_call": {"operation": "fmt", "arguments": [{"key": "v", "value_path": "/other/v"}]}}])


def test_state_choice_calls_may_reference_state_but_not_undeclared_ports() -> None:
    """State choice calls may reference state but not undeclared ports."""
    with pytest.raises(ValidationError, match="references 'other'"):
        _widget("STATE_CHOICE", state_call={"operation": "pick", "arguments": [{"key": "s", "value_path": "/other"}]})
    _widget("STATE_CHOICE", dependencies=["other"], state_call={"operation": "pick", "arguments": [{"key": "s", "value_path": "/other"}, {"key": "t", "value_path": "/state/x"}]})
    with pytest.raises(ValidationError, match="StateAccessor 0 references 'other'"):
        _widget("STATE_CHOICE", state_path="/x", state_accessors=[{"option_key": "LABEL", "call": {"operation": "f", "arguments": [{"key": "v", "value_path": "/other"}]}}])


def test_state_accessor_is_static_or_computed() -> None:
    """State accessor is static or computed."""
    with pytest.raises(ValidationError, match="either path or call"):
        imodels.StateAccessorInputModel(option_key="LABEL", path="/x", call={"operation": "f"})
    imodels.StateAccessorInputModel(option_key="LABEL")


def test_custom_widgets_round_trip_to_output_models() -> None:
    """Custom widgets round trip to output models, member instances included."""
    widget = _widget("CUSTOM", component="Knob", dependencies=["bar"], props=[{"key": "value", "dynamic_value": {"path": "/value"}}], fallback={"kind": "STRING"})
    port = imodels.ArgPortInputModel(key="foo", kind="STRING", nullable=False, widget=widget)  # instance, as strawberry hands it over
    out = omodels.ArgPortModel(**port.model_dump())
    assert isinstance(out.widget, omodels.CustomAssignWidgetModel)
    assert out.widget.component == "Knob"
    assert out.widget.props[0].dynamic_value.path == "/value"
    assert isinstance(out.widget.fallback, omodels.StringWidgetModel)

    ret = imodels.ReturnPortInputModel(key="out", kind="STRING", nullable=False, widget={"kind": "CUSTOM", "component": "Gauge"})
    out = omodels.ReturnPortModel(**ret.model_dump())
    assert isinstance(out.widget, omodels.CustomReturnWidgetModel)
    assert out.widget.kind == "CUSTOM"


def test_return_widget_members_are_strict() -> None:
    """Return widget members are strict."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RETURN.validate_python({"kind": "CHOICE", "component": "X"})
    with pytest.raises(ValidationError, match="agent calls are not allowed"):
        RETURN.validate_python({"kind": "CUSTOM", "component": "X", "props": [{"key": "k", "agent_call": {"dependency": "d", "operation": "o"}}]})


def _definition(**overrides: object) -> imodels.DefinitionInputModel:
    base = {"key": "x", "name": "x", "kind": "FUNCTION", "args": [], "returns": []}
    return imodels.DefinitionInputModel(**{**base, **overrides})


def test_widget_dependencies_and_follow_value_are_port_paths() -> None:
    """Widget dependencies and follow_value are resolved like validator dependencies."""
    with pytest.raises(ValidationError, match="Widget CUSTOM in port foo has invalid dependency: nope"):
        _definition(args=[_port("foo", widget={"kind": "CUSTOM", "component": "Knob", "dependencies": ["nope"]}).model_dump()])
    with pytest.raises(ValidationError, match="Widget SLIDER in port foo follows an unknown port: nope"):
        _definition(args=[_port("foo", "INT", widget={"kind": "SLIDER", "follow_value": "nope"}).model_dump()])
    with pytest.raises(ValidationError, match=r"Widget STRING in port foo \(fallback 1\) follows an unknown port: nope"):
        _definition(args=[_port("foo", widget={"kind": "CUSTOM", "component": "Knob", "fallback": {"kind": "STRING", "follow_value": "nope"}}).model_dump()])
    _definition(
        args=[
            _port("bar", "MODEL", children=[_port("baz").model_dump()]).model_dump(),
            _port("foo", widget={"kind": "CUSTOM", "component": "Knob", "dependencies": ["bar..baz"], "follow_value": "bar..baz"}).model_dump(),
        ]
    )


def _implementation(dependencies: list[dict], widget: dict) -> imodels.ImplementationInputModel:
    definition = _definition(args=[_port("foo", "STRUCTURE", identifier="@x/y", widget=widget).model_dump()])
    return imodels.ImplementationInputModel(interface="i", definition=definition, dependencies=dependencies)


def test_proxy_and_state_choice_targets_must_be_declared_dependencies() -> None:
    """Proxy and state choice targets must be declared dependencies."""
    stage = {"key": "stage", "action_dependencies": [{"key": "move"}]}
    with pytest.raises(ValidationError, match="STATE_CHOICE in port foo names undeclared agent dependency 'camera'"):
        _implementation([stage], {"kind": "STATE_CHOICE", "state_path": "/p", "dependency": "camera"})
    _implementation([stage], {"kind": "STATE_CHOICE", "state_path": "/p", "dependency": "stage"})
    with pytest.raises(ValidationError, match="PROXY in port foo names undeclared agent dependency 'camera'"):
        _implementation([stage], {"kind": "PROXY", "target_port": "p", "target_action": "move", "target_dependency": "camera"})
    with pytest.raises(ValidationError, match=r"targets action 'fire', which dependency 'stage' does not declare \(it declares \['move'\]\)"):
        _implementation([stage], {"kind": "PROXY", "target_port": "p", "target_action": "fire", "target_dependency": "stage"})
    _implementation([stage], {"kind": "PROXY", "target_port": "p", "target_action": "move", "target_dependency": "stage"})
    _implementation([{"key": "free"}], {"kind": "PROXY", "target_port": "p", "target_action": "anything", "target_dependency": "free"})


def test_placeholder_round_trips_for_search_and_choice_widgets() -> None:
    """`placeholder` was accepted for SEARCH and CHOICE but silently dropped on read-back."""
    for port in (
        _port(kind="STRUCTURE", identifier="@x/y", widget={"kind": "SEARCH", "query": SEARCH_QUERY, "ward": "mikro", "placeholder": "pick one"}),
        _port(choices=[{"value": "a", "label": "A"}], widget={"kind": "CHOICE", "placeholder": "pick one"}),
    ):
        assert omodels.ArgPortModel(**port.model_dump()).widget.placeholder == "pick one"


def test_widget_default_needs_a_selector_and_a_widget() -> None:
    """Widget default needs a selector and a widget."""
    with pytest.raises(ValidationError, match="kind and/or an identifier"):
        imodels.WidgetDefaultInputModel(widget={"kind": "SLIDER"})
    with pytest.raises(ValidationError, match="widget and/or a return_widget"):
        imodels.WidgetDefaultInputModel(kind="STRUCTURE", identifier="@mikro/image")
    default = imodels.WidgetDefaultInputModel(identifier="@mikro/image", widget={"kind": "CUSTOM", "component": "ImagePicker"}, return_widget={"kind": "CUSTOM", "component": "ImageView"})
    assert default.selector == (None, "@mikro/image")
    assert imodels.WidgetDefaultInputModel(kind="FLOAT", widget={"kind": "SLIDER", "min": 0, "max": 1}).selector == ("FLOAT", None)
    out = omodels.WidgetDefaultModel(**default.model_dump())
    assert isinstance(out.widget, omodels.CustomAssignWidgetModel) and isinstance(out.return_widget, omodels.CustomReturnWidgetModel)


def test_custom_effect_carries_only_its_call() -> None:
    """Custom effect carries only its call."""
    effect = imodels.EffectInputModel(kind="CUSTOM", call={"operation": "shake"}, dependencies=[])
    assert set(effect.model_dump()) == {"kind", "call", "dependencies", "message", "fade", "source"}


def test_optimistic_pointer_is_static_or_computed_from_args() -> None:
    """Optimistic pointer is static or computed from args."""
    imodels.OptimisticInputModel(state="stage", path="/position")
    imodels.OptimisticInputModel(state="stage", path_call={"operation": "sel", "arguments": [{"key": "a", "value_path": "/args/axis"}]})
    with pytest.raises(ValidationError, match="exactly one of path or path_call"):
        imodels.OptimisticInputModel(state="stage")
    with pytest.raises(ValidationError, match="references 'state'"):
        imodels.OptimisticInputModel(state="stage", path_call={"operation": "sel", "arguments": [{"key": "a", "value_path": "/state/x"}]})


def test_window_function_is_an_enum() -> None:
    """Window function is an enum."""
    with pytest.raises(ValidationError):
        imodels.WindowInputModel(window_function="average")
    window = imodels.WindowInputModel(window_function="MEAN", label="avg")
    track = omodels.TrackModel(state_key="s", value_key="v", windows=[window.model_dump()])
    assert track.windows[0].window_function == "MEAN"


def test_a_search_widget_may_ask_for_the_ports_requires_but_no_filter_may_take_its_name() -> None:
    """`$requires` is filled by the frontend from the port the widget sits on, so a query may declare it freely and a filter port may not be keyed by it."""
    query = "query S($search: String, $values: [ID!], $requires: [DescriptorConstraintInput!]) { options: lenses(filters: {search: $search, ids: $values, requires: $requires}) { value: id label } }"
    assert _widget("SEARCH", query=query, ward="mikro").filters is None
    with pytest.raises(ValidationError, match="reserved key 'requires'"):
        _widget("SEARCH", query=query, ward="mikro", filters=[{"key": "requires", "kind": "STRING", "nullable": False}])
