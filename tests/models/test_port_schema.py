"""The port schema: keys, per-kind structure, defaults, port groups, choices, descriptors, effects, and assignment values."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels
from rekuest_core.objects import models as omodels
from rekuest_core.values import validate_assignment_args, value_mismatch


def _port(key: str = "foo", kind: str = "STRING", **extra: object) -> dict:
    return {"key": key, "kind": kind, "nullable": False, **extra}


def _arg(**fields: object) -> imodels.ArgPortInputModel:
    return imodels.ArgPortInputModel(**_port(**fields))


def _definition(**overrides: object) -> imodels.DefinitionInputModel:
    base = {"key": "x", "name": "x", "kind": "FUNCTION", "args": [], "returns": []}
    return imodels.DefinitionInputModel(**{**base, **overrides})


STRUCTURE = _port("img", "STRUCTURE", identifier="@mikro/image")


# --------------------------------------------------------------------------- keys


@pytest.mark.parametrize("key", ["", "value", "a..b"])
def test_bad_keys_are_rejected(key: str) -> None:
    """Empty, reserved and separator-bearing keys are rejected."""
    with pytest.raises(ValidationError):
        _arg(key=key)


def test_item_key_is_allowed_for_list_children() -> None:
    """The conventional '...' item key is the one key that may contain the separator."""
    port = _arg(key="items", kind="LIST", children=[_port("...")])
    assert port.children[0].key == "..."


def test_root_and_child_keys_are_unique_and_args_returns_do_not_overlap() -> None:
    """Root and child keys are unique and args returns do not overlap."""
    with pytest.raises(ValidationError, match="args: duplicate key 'a'"):
        _definition(args=[_port("a"), _port("a")])
    with pytest.raises(ValidationError, match="returns: duplicate key 'r'"):
        _definition(returns=[_port("r"), _port("r")])
    with pytest.raises(ValidationError, match=r"args and returns share the keys \['a'\]"):
        _definition(args=[_port("a")], returns=[_port("a")])
    with pytest.raises(ValidationError, match="children: duplicate key 'f'"):
        _arg(key="m", kind="MODEL", children=[_port("f"), _port("f")])


# --------------------------------------------------------------------------- per-kind structure


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        ("STRUCTURE", {"identifier": "@mikro/image"}),
        ("MEMORY_STRUCTURE", {"identifier": "@mikro/image"}),
        ("INTERFACE", {"identifier": "@mikro/viewable"}),
        ("LIST", {"children": [_port("...")]}),
        ("DICT", {"children": [_port("...", "INT")]}),
        ("DICT", {"children": [_port("width", "INT"), _port("height", "INT")]}),
        ("UNION", {"children": [_port("a", "INT"), _port("b", "STRING")]}),
        ("MODEL", {"children": [_port("x", "INT")]}),
        ("MODEL", {"children": [_port("x", "INT")], "identifier": "@mikro/point"}),
        ("ENUM", {"choices": [{"value": "a", "label": "A"}]}),
        ("ENUM", {"choices": [{"value": "a", "label": "A"}], "identifier": "@literal/a"}),
        ("INT", {"choices": [{"value": 1, "label": "one"}]}),
        ("DATE", {}),
        ("BOOL", {}),
    ],
)
def test_well_formed_ports_are_accepted(kind: str, extra: dict) -> None:
    """Well formed ports are accepted."""
    _arg(kind=kind, **extra)


@pytest.mark.parametrize(
    ("kind", "extra", "message"),
    [
        ("STRUCTURE", {}, "must declare an identifier"),
        ("MEMORY_STRUCTURE", {}, "must declare an identifier"),
        ("INTERFACE", {}, "must declare an identifier"),
        ("STRUCTURE", {"identifier": "mikro/image"}, "not of the form @package/key"),
        ("STRUCTURE", {"identifier": "@mikro"}, "not of the form @package/key"),
        ("INT", {"identifier": "@mikro/image"}, "must not declare an identifier"),
        ("ENUM", {"choices": [{"value": "a", "label": "A"}], "identifier": "literal.a"}, "not of the form @package/key"),
        ("STRUCTURE", {"identifier": "@mikro/image", "children": [_port("c")]}, "must not have children"),
        ("LIST", {}, "exactly one child"),
        ("LIST", {"children": [_port("a"), _port("b")]}, "exactly one child"),
        ("DICT", {}, "at least 1 children"),
        ("DICT", {"children": [_port("..."), _port("named")]}, "either homogeneous"),
        ("UNION", {"children": [_port("a")]}, "at least 2 children"),
        ("MODEL", {}, "at least 1 children"),
        ("ENUM", {}, "must declare choices"),
        ("BOOL", {"choices": [{"value": True, "label": "yes"}]}, "must not declare choices"),
        ("STRING", {"choices": [{"value": "a", "label": "A"}, {"value": "a", "label": "B"}]}, "choices: duplicate value 'a'"),
        ("STRING", {"colour": "red"}, "Extra inputs are not permitted"),
    ],
)
def test_malformed_ports_are_rejected(kind: str, extra: dict, message: str) -> None:
    """Malformed ports are rejected."""
    with pytest.raises(ValidationError, match=message):
        _arg(kind=kind, **extra)


# --------------------------------------------------------------------------- defaults


@pytest.mark.parametrize(
    ("kind", "extra", "default"),
    [
        ("INT", {}, 3),
        ("FLOAT", {}, 3),
        ("FLOAT", {}, 2.5),
        ("STRING", {}, "x"),
        ("BOOL", {}, True),
        ("DATE", {}, "2026-09-02"),
        ("DATE", {}, "2026-09-02T10:00:00"),
        ("LIST", {"children": [_port("...", "INT")]}, [1, 2]),
        ("DICT", {"children": [_port("...", "INT")]}, {"a": 1}),
        ("DICT", {"children": [_port("width", "INT")]}, {"width": 1, "other": "free"}),
        ("MODEL", {"children": [_port("x", "INT"), _port("y", "STRING", nullable=True)]}, {"x": 1}),
        ("ENUM", {"choices": [{"value": "a", "label": "A"}]}, "a"),
        ("STRUCTURE", {"identifier": "@mikro/image"}, "42"),
        ("QUANTITY", {"reference_unit": "volt"}, 3.3),
        ("QUANTITY", {"reference_unit": "volt"}, {"value": 3.3, "unit": "mV"}),
        ("UNION", {"children": [_port("a", "INT"), _port("b", "STRING")]}, "text"),
    ],
)
def test_defaults_that_fit_the_kind_are_accepted(kind: str, extra: dict, default: object) -> None:
    """Defaults that fit the kind are accepted."""
    assert _arg(kind=kind, default=default, **extra).default == default


@pytest.mark.parametrize(
    ("kind", "extra", "default", "message"),
    [
        ("INT", {}, "3", "expected an INT"),
        ("INT", {}, True, "expected an INT"),
        ("FLOAT", {}, "x", "expected a FLOAT"),
        ("STRING", {}, 3, "expected a STRING"),
        ("BOOL", {}, 1, "expected a BOOL"),
        ("DATE", {}, "yesterday", "not an ISO-8601 date"),
        ("LIST", {"children": [_port("...", "INT")]}, [1, "x"], r"foo\[1\]: expected an INT"),
        ("DICT", {"children": [_port("...", "INT")]}, {"a": "x"}, r"foo\['a'\]: expected an INT"),
        ("MODEL", {"children": [_port("x", "INT")]}, {"y": 1}, r"unknown fields \['y'\]"),
        ("MODEL", {"children": [_port("x", "INT")]}, {}, "foo.x: required field is missing"),
        ("ENUM", {"choices": [{"value": "a", "label": "A"}]}, "b", "not one of the choices"),
        ("STRUCTURE", {"identifier": "@mikro/image"}, {"id": 1}, "expected a STRUCTURE id"),
        ("QUANTITY", {"reference_unit": "volt"}, {"unit": "mV"}, "needs a numeric `value`"),
        ("UNION", {"children": [_port("a", "INT"), _port("b", "STRING")]}, 2.5, "matches none of the UNION variants"),
    ],
)
def test_defaults_that_do_not_fit_are_rejected(kind: str, extra: dict, default: object, message: str) -> None:
    """Defaults that do not fit are rejected."""
    with pytest.raises(ValidationError, match=message):
        _arg(kind=kind, default=default, **extra)


def test_return_ports_carry_no_default_and_no_validators() -> None:
    """Return ports carry no default and no validators."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        imodels.ReturnPortInputModel(**_port("out"), default="x")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        imodels.ReturnPortInputModel(**_port("out"), validators=[])


# --------------------------------------------------------------------------- port groups, choices, effects, descriptors


def test_port_groups_reference_root_args_once() -> None:
    """Port groups reference root args once."""
    args = [_port("a"), _port("b")]
    with pytest.raises(ValidationError, match="port_groups: duplicate key 'g'"):
        _definition(args=args, port_groups=[{"key": "g"}, {"key": "g"}])
    with pytest.raises(ValidationError, match="lists unknown arg 'zzz'"):
        _definition(args=args, port_groups=[{"key": "g", "ports": ["zzz"]}])
    with pytest.raises(ValidationError, match="in both port groups 'g' and 'h'"):
        _definition(args=args, port_groups=[{"key": "g", "ports": ["a"]}, {"key": "h", "ports": ["a"]}])
    definition = _definition(args=args, port_groups=[{"key": "g", "ports": ["a"]}, {"key": "h", "ports": ["b"]}])
    assert definition.port_groups[0].title is None and definition.port_groups[0].effects is None


def test_choice_values_are_json_and_typed_against_the_kind() -> None:
    """Choice values are JSON on every layer and checked against the kind through the default."""
    port = _arg(kind="INT", choices=[{"value": 1, "label": "one"}, {"value": 2, "label": "two"}], default=2)
    out = omodels.ArgPortModel(**port.model_dump())
    assert [choice.value for choice in out.choices] == [1, 2]
    with pytest.raises(ValidationError, match="not one of its choices"):
        _arg(kind="INT", choices=[{"value": 1, "label": "one"}], default=3)


def test_effect_kind_fields() -> None:
    """A MESSAGE effect needs a message; fade is HIDE-only."""
    call = {"operation": "gt"}
    with pytest.raises(ValidationError, match="MESSAGE effect requires a message"):
        imodels.EffectInputModel(kind="MESSAGE", call=call)
    with pytest.raises(ValidationError, match="must not set fade"):
        imodels.EffectInputModel(kind="MESSAGE", message="m", call=call, fade=False)
    imodels.EffectInputModel(kind="HIDE", call=call, fade=False)


def test_descriptor_operator_and_value_agree() -> None:
    """Descriptor operator and value agree."""
    with pytest.raises(ValidationError, match="IN needs a list value"):
        imodels.RequiresInputModel(key="axes", operator="IN", value="c")
    with pytest.raises(ValidationError, match="GTE needs a numeric value"):
        imodels.ProvidesInputModel(key="size", operator="GTE", value="big")
    with pytest.raises(ValidationError, match="EXISTS takes no value"):
        imodels.RequiresInputModel(key="axes", operator="EXISTS", value="c")
    imodels.RequiresInputModel(key="axes", operator="EXISTS")
    imodels.RequiresInputModel(key="axes", operator="IN", value=["c", "z"])
    assert imodels.RequiresInputModel(key="n", operator="LTE", value=3).operator.value == "LTE"


def test_definition_hash_covers_kind_and_port_groups() -> None:
    """Changing only the kind or the port groups changes the definition hash."""
    base = _definition(args=[_port("a")])
    assert _definition(args=[_port("a")], kind="GENERATOR").unique_hash != base.unique_hash
    assert _definition(args=[_port("a")], port_groups=[{"key": "g", "ports": ["a"]}]).unique_hash != base.unique_hash


# --------------------------------------------------------------------------- assignment values


def _ports(*ports: dict) -> list[omodels.ArgPortModel]:
    return [omodels.ArgPortModel(**imodels.ArgPortInputModel(**port).model_dump()) for port in ports]


def test_assignment_args_are_validated_against_the_ports() -> None:
    """Assignment args are validated against the ports."""
    ports = _ports(_port("n", "INT"), _port("opt", "STRING", nullable=True), _port("d", "FLOAT", default=1.0), STRUCTURE)
    ref = {"__identifier": "@mikro/image", "object": "7"}
    validate_assignment_args(ports, {"n": 1, "img": ref})
    validate_assignment_args(ports, {"n": 1, "opt": None, "d": 2, "img": {"__identifier": "@mikro/image", "object": 7}})
    with pytest.raises(ValueError, match=r"Unknown arguments \['zzz'\]"):
        validate_assignment_args(ports, {"n": 1, "img": ref, "zzz": 1})
    with pytest.raises(ValueError, match="Argument 'n' is required and has no default"):
        validate_assignment_args(ports, {"img": ref})
    with pytest.raises(ValueError, match="Argument n: expected an INT, got str"):
        validate_assignment_args(ports, {"n": "1", "img": ref})


def test_assignment_structure_args_must_carry_the_reference_envelope() -> None:
    """A structure argument is an envelope, not a bare id, and it names its port's identifier."""
    ports = _ports(_port("n", "INT"), STRUCTURE)
    with pytest.raises(ValueError, match="expected a STRUCTURE reference"):
        validate_assignment_args(ports, {"n": 1, "img": "7"})
    with pytest.raises(ValueError, match="missing its `object` key"):
        validate_assignment_args(ports, {"n": 1, "img": {"__identifier": "@mikro/image"}})
    with pytest.raises(ValueError, match="missing its `__identifier` key"):
        validate_assignment_args(ports, {"n": 1, "img": {"object": "7"}})
    with pytest.raises(ValueError, match="identifier mismatch"):
        validate_assignment_args(ports, {"n": 1, "img": {"__identifier": "@mikro/other", "object": "7"}})
    with pytest.raises(ValueError, match="`object` must be a str or int id"):
        validate_assignment_args(ports, {"n": 1, "img": {"__identifier": "@mikro/image", "object": {"a": 1}}})


def test_structure_port_default_is_still_a_bare_id() -> None:
    """A default is declared in the definition, not sent by a client, so it stays a bare id."""
    (port,) = _ports(STRUCTURE)
    assert value_mismatch(port, "7") is None
    assert value_mismatch(port, {"__identifier": "@mikro/image", "object": "7"}) is not None


def test_value_mismatch_recurses_into_containers() -> None:
    """Value mismatch recurses into containers."""
    (port,) = _ports(_port("m", "MODEL", children=[_port("xs", "LIST", children=[_port("...", "INT")]), _port("s", "STRING", nullable=True)]))
    assert value_mismatch(port, {"xs": [1, 2]}) is None
    assert value_mismatch(port, {"xs": [1, None]}) == "m.xs[1]: null is not allowed"
    assert value_mismatch(port, {"xs": [1], "s": 3}) == "m.s: expected a STRING, got int"
