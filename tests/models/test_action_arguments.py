"""Shape rules for blok call arguments: exactly one binding per argument, keyed maps, unkeyed lists."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels


@pytest.mark.parametrize(
    "binding",
    [
        {"value_literal": 3},
        {"value_path": "/value"},
        {"util_call": {"operation": "noop"}},
        {"agent_call": {"dependency": "stage", "operation": "move"}},
        {"value_list": [{"value_literal": 1}]},
        {"value_dict": [{"key": "a", "value_literal": 1}]},
    ],
)
def test_each_single_binding_is_accepted(binding: dict) -> None:
    """Every binding form is valid on its own."""
    imodels.ActionArgumentInputModel(key="a", **binding)


def test_unbound_argument_is_null() -> None:
    """An argument without any binding is null (blok language v2)."""
    assert imodels.ActionArgumentInputModel(key="a").value_literal is None


def test_doubly_bound_argument_is_rejected() -> None:
    """Resolution order between two bindings would be left to the client."""
    with pytest.raises(ValidationError, match="at most one of"):
        imodels.ActionArgumentInputModel(key="a", value_literal=1, value_path="/value")


def test_value_dict_entries_need_unique_keys() -> None:
    """Value dict entries need unique keys."""
    with pytest.raises(ValidationError, match="every entry must carry a key"):
        imodels.ActionArgumentInputModel(key="a", value_dict=[{"value_literal": 1}])
    with pytest.raises(ValidationError, match="duplicate key 'x'"):
        imodels.ActionArgumentInputModel(key="a", value_dict=[{"key": "x", "value_literal": 1}, {"key": "x", "value_literal": 2}])


def test_value_list_entries_must_not_carry_keys() -> None:
    """Value list entries must not carry keys."""
    with pytest.raises(ValidationError, match="must not carry a key"):
        imodels.ActionArgumentInputModel(key="a", value_list=[{"key": "x", "value_literal": 1}])


def test_call_arguments_need_unique_non_empty_keys() -> None:
    """Call arguments need unique non empty keys."""
    with pytest.raises(ValidationError, match="every entry must carry a key"):
        imodels.UtilCallInputModel(operation="gt", arguments=[{"value_literal": 1}])
    with pytest.raises(ValidationError, match="duplicate key 'a'"):
        imodels.UtilCallInputModel(operation="gt", arguments=[{"key": "a", "value_literal": 1}, {"key": "a", "value_literal": 2}])
    imodels.UtilCallInputModel(operation="gt", arguments=[{"key": "a", "value_literal": 1}, {"key": "b", "value_path": "/value"}])
