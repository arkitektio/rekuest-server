"""Tests for the blok ``call`` primitive on port effects and validators.

These exercise the pydantic layer of ``rekuest_core`` directly (no database): shape validation,
purity rules (no agent calls, value_paths restricted to ``dependencies`` + ``value``), and the
round trip from input models through ``model_dump`` into the output models.
"""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels
from rekuest_core.objects import models as omodels


def _call(*arguments: dict) -> dict:
    return {"operation": "gt", "arguments": list(arguments)}


def test_validator_accepts_call_referencing_own_value() -> None:
    """Validator accepts call referencing own value."""
    validator = imodels.ValidatorInputModel(
        call=_call({"key": "a", "value_path": "/value"}), dependencies=[]
    )
    assert validator.call.operation == "gt"
    assert validator.call.arguments[0].value_path == "/value"


def test_validator_accepts_value_path_into_declared_dependency() -> None:
    """Validator accepts value path into declared dependency."""
    validator = imodels.ValidatorInputModel(
        call=_call(
            {"key": "a", "value_path": "/other/x"}, {"key": "b", "value_literal": 3}
        ),
        dependencies=["other"],
    )
    assert validator.dependencies == ["other"]


def test_validator_rejects_value_path_outside_dependencies() -> None:
    """Validator rejects value path outside dependencies."""
    with pytest.raises(ValidationError, match="'other' via value_path"):
        imodels.ValidatorInputModel(
            call=_call({"key": "a", "value_path": "/other/x"}), dependencies=[]
        )


def test_validator_rejects_nested_agent_call() -> None:
    """Validator rejects nested agent call."""
    with pytest.raises(ValidationError, match="must be pure"):
        imodels.ValidatorInputModel(
            call=_call(
                {
                    "key": "a",
                    "value_list": [
                        {"agent_call": {"dependency": "stage", "operation": "move"}}
                    ],
                }
            ),
            dependencies=[],
        )


def test_validator_rejects_missing_or_empty_operation() -> None:
    """Validator rejects missing or empty operation."""
    with pytest.raises(ValidationError):
        imodels.ValidatorInputModel(call={"arguments": []}, dependencies=[])
    with pytest.raises(ValidationError):
        imodels.ValidatorInputModel(call={"operation": ""}, dependencies=[])


def test_validator_checks_value_paths_nested_in_util_calls() -> None:
    """Validator checks value paths nested in util calls."""
    nested = {
        "key": "a",
        "util_call": _call({"key": "inner", "value_path": "hidden/x"}),
    }
    with pytest.raises(ValidationError, match="'hidden'"):
        imodels.ValidatorInputModel(call=_call(nested), dependencies=["other"])
    imodels.ValidatorInputModel(call=_call(nested), dependencies=["hidden"])


def test_effect_rejects_value_path_outside_dependencies() -> None:
    """Effect rejects value path outside dependencies."""
    with pytest.raises(ValidationError, match="Effect HIDE"):
        imodels.EffectInputModel(
            kind="HIDE",
            call=_call({"key": "a", "value_path": "/other"}),
            dependencies=[],
        )


def test_effect_round_trips_through_arg_port_models() -> None:
    """Effect round trips through arg port models."""
    port = imodels.ArgPortInputModel(
        key="foo",
        kind="STRING",
        nullable=False,
        effects=[
            imodels.EffectInputModel(
                kind="HIDE",
                call=_call({"key": "a", "value_path": "/other"}),
                dependencies=["other"],
            )
        ],
        validators=[
            imodels.ValidatorInputModel(
                call=_call({"key": "a", "value_path": "value"}), label="positive"
            )
        ],
    )

    read_back = omodels.ArgPortModel(**port.model_dump())

    assert read_back.effects[0].kind == "HIDE"
    assert read_back.effects[0].call.operation == "gt"
    assert read_back.effects[0].call.arguments[0].value_path == "/other"
    assert read_back.validators[0].call.operation == "gt"
    assert read_back.validators[0].label == "positive"


def test_definition_rejects_dependency_on_unknown_port() -> None:
    """Definition rejects dependency on unknown port."""
    with pytest.raises(ValidationError, match="invalid dependency: missing"):
        imodels.DefinitionInputModel(
            key="x",
            name="x",
            kind="FUNCTION",
            args=[
                imodels.ArgPortInputModel(
                    key="foo",
                    kind="STRING",
                    nullable=False,
                    effects=[
                        imodels.EffectInputModel(
                            kind="HIDE", call=_call(), dependencies=["missing"]
                        )
                    ],
                )
            ],
            returns=[],
        )


def _port(key: str, **extra: object) -> dict:
    return {"key": key, "kind": "STRING", "nullable": False, **extra}


def _definition(**overrides: object) -> imodels.DefinitionInputModel:
    base = {"key": "x", "name": "x", "kind": "FUNCTION", "args": [], "returns": []}
    return imodels.DefinitionInputModel(**{**base, **overrides})


def test_nested_dependency_path_resolves_through_children() -> None:
    """A dependency 'foo..bar' names the child 'bar' of port 'foo'."""
    validator = imodels.ValidatorInputModel(
        call=_call({"key": "a", "value_path": "/foo..bar/x"}), dependencies=["foo..bar"]
    )
    _definition(
        args=[
            _port("foo", kind="MODEL", children=[_port("bar")]),
            _port("target", validators=[validator]),
        ]
    )


def test_unresolvable_nested_dependency_path_is_rejected() -> None:
    """Unresolvable nested dependency path is rejected."""
    validator = imodels.ValidatorInputModel(call=_call(), dependencies=["foo..nope"])
    with pytest.raises(ValidationError, match="invalid dependency: foo..nope"):
        _definition(
            args=[
                _port("foo", kind="MODEL", children=[_port("bar")]),
                _port("target", validators=[validator]),
            ]
        )


def test_return_port_and_port_group_effects_are_checked() -> None:
    """Return port and port group effects are checked."""
    effect = imodels.EffectInputModel(
        kind="HIDE", call=_call(), dependencies=["missing"]
    )
    with pytest.raises(ValidationError, match="invalid dependency: missing"):
        _definition(returns=[_port("out", effects=[effect])])
    with pytest.raises(
        ValidationError, match="port group grp has invalid dependency: missing"
    ):
        _definition(
            port_groups=[
                {
                    "key": "grp",
                    "title": None,
                    "description": None,
                    "effects": [effect],
                    "ports": [],
                }
            ]
        )


def test_dependency_inside_child_port_is_checked() -> None:
    """Dependency inside child port is checked."""
    validator = imodels.ValidatorInputModel(call=_call(), dependencies=["missing"])
    with pytest.raises(
        ValidationError, match="in port foo..bar has invalid dependency: missing"
    ):
        _definition(
            args=[
                _port(
                    "foo", kind="MODEL", children=[_port("bar", validators=[validator])]
                )
            ]
        )


def test_port_named_value_is_rejected() -> None:
    """'value' names the port's own value inside calls, so no port may be keyed that way."""
    with pytest.raises(ValidationError, match="'value' is a reserved port key"):
        _definition(args=[_port("value")])


def test_source_rides_along_without_being_interpreted() -> None:
    """``source`` is stored and read back verbatim; the server never parses it."""
    validator = imodels.ValidatorInputModel(
        call=_call({"key": "a", "value_path": "/value"}),
        source="value > 3  # anything goes",
    )
    assert (
        omodels.ValidatorModel(**validator.model_dump()).source
        == "value > 3  # anything goes"
    )
    effect = imodels.EffectInputModel(
        kind="HIDE", call=_call({"key": "a", "value_path": "/value"}), dependencies=[]
    )
    assert omodels.EffectModel(**effect.model_dump()).source is None
