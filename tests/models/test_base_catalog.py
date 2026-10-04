"""The base catalog: the manifest, its merge with registered UI catalogs, and the shadowing rule."""

import pytest

from facade import models
from facade.catalog_validation import (
    UNKNOWN_OPERATION,
    check_extension_does_not_shadow_base,
    resolve_operations,
    validate_calls_against_catalog,
)
from rekuest_core.catalogs import BASE_CATALOG_VERSION, base_operation_names, base_operations, load_base_catalog
from rekuest_core.inputs import models as imodels

V1_NAMES = {
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "and",
    "or",
    "not",
    "if",
    "is_null",
    "is_set",
    "len",
    "len_between",
    "is_empty",
    "contains",
    "in",
    "matches",
    "starts_with",
    "ends_with",
    "get",
    "coalesce",
    "add",
    "sub",
    "mul",
    "div",
    "mod",
    "neg",
}


def test_manifest_loads_and_is_cached() -> None:
    """Manifest loads and is cached."""
    manifest = load_base_catalog()
    assert manifest is load_base_catalog()
    assert manifest.name == "base"
    assert manifest.version == BASE_CATALOG_VERSION == 1


def test_manifest_names_match_the_v1_vocabulary() -> None:
    """Manifest names match the v1 vocabulary exactly."""
    assert base_operation_names() == frozenset(V1_NAMES)


def test_manifest_argument_order_is_positional_order() -> None:
    """Argument order is the order positional call arguments map onto."""
    ops = base_operations()
    assert [a.key for a in ops["gt"].arguments] == ["a", "b"]
    assert [a.key for a in ops["len_between"].arguments] == ["value", "min", "max"]
    assert ops["len_between"].arguments[2].required is False
    assert [a.key for a in ops["if"].arguments] == ["condition", "then", "otherwise"]
    for op in ops.values():
        assert op.description, f"{op.name} needs a description"
        for argument in op.arguments:
            assert argument.description, f"{op.name}.{argument.key} needs a description"


def test_boolean_operations_return_bool() -> None:
    """Everything a validator or effect can use returns BOOL; the value-shaped ones do not."""
    ops = base_operations()
    non_bool = {name for name, op in ops.items() if op.returns != "BOOL"}
    assert non_bool == {"if", "len", "get", "coalesce", "add", "sub", "mul", "div", "mod", "neg"}


def test_parameter_names_are_python_keyword_argument_names() -> None:
    """Every argument key can be written as a Python keyword argument (so `else` is `otherwise`)."""
    import keyword

    for op in base_operations().values():
        for argument in op.arguments:
            assert argument.key.isidentifier() and not keyword.iskeyword(argument.key), f"{op.name}.{argument.key}"


def test_resolve_operations_without_catalog_is_base() -> None:
    """Resolve operations without catalog is base."""
    assert set(resolve_operations([])) == V1_NAMES


@pytest.mark.django_db
def test_resolve_operations_unions_extension() -> None:
    """A registered catalog adds to base."""
    org = __import__("authentikate.models", fromlist=["Organization"]).Organization.objects.create(slug="base-cat-org")
    catalog = models.UICatalog.objects.create(name="electron", organization=org, operations=[{"name": "clamp", "arguments": [{"key": "v", "kind": "FLOAT"}], "returns": "FLOAT"}])
    resolved = resolve_operations([catalog])
    assert set(resolved) == V1_NAMES | {"clamp"}
    assert resolved["gt"] is base_operations()["gt"]


def test_base_version_names() -> None:
    """``base`` and ``base@N`` are recognised as base references."""
    from rekuest_core.catalogs import BASE_CATALOG_ID, base_version_named

    assert BASE_CATALOG_ID == "base@1"
    assert base_version_named("base") == 1 and base_version_named("base@1") == 1 and base_version_named("base@7") == 7
    assert base_version_named("electron") is None and base_version_named("base@x") is None


def test_extension_cannot_shadow_base() -> None:
    """Extension cannot shadow base."""
    gt = imodels.CatalogOperationInputModel(name="gt", arguments=[], returns="BOOL")
    fmt = imodels.CatalogOperationInputModel(name="fmt", arguments=[], returns="STRING")
    check_extension_does_not_shadow_base([fmt], "electron")
    with pytest.raises(ValueError, match=r"catalog 'electron' cannot redefine base operations \['gt'\]"):
        check_extension_does_not_shadow_base([fmt, gt], "electron")


def _call(operation: str, *arguments: dict) -> imodels.UtilCallInputModel:
    return imodels.UtilCallInputModel(operation=operation, arguments=list(arguments))


def test_unknown_operation_is_a_warning_and_argument_mismatch_an_error() -> None:
    """Unknown operation is a warning, argument mismatch on a known operation an error."""
    warnings = validate_calls_against_catalog(None, [_call("fizz", {"key": "a", "value_path": "/value"})], "Validator x")
    assert [w.code for w in warnings] == [UNKNOWN_OPERATION]
    assert "'fizz'" in warnings[0].message and warnings[0].path == "Validator x"

    with pytest.raises(ValueError, match=r"'between' does not accept arguments \['a', 'b'\]"):
        validate_calls_against_catalog(None, [_call("between", {"key": "a", "value_path": "/value"}, {"key": "b", "value_literal": 1})], "Validator x")
    with pytest.raises(ValueError, match=r"'gt' requires arguments \['b'\]"):
        validate_calls_against_catalog(None, [_call("gt", {"key": "a", "value_path": "/value"})], "Validator x")


def test_nested_calls_are_checked_and_optional_arguments_may_be_omitted() -> None:
    """Nested calls are checked; optional arguments may be omitted."""
    nested = _call("not", {"key": "a", "util_call": _call("fizz")})
    assert [w.code for w in validate_calls_against_catalog(None, [nested], "Effect")] == [UNKNOWN_OPERATION]
    assert validate_calls_against_catalog(None, [_call("len_between", {"key": "value", "value_path": "/value"}, {"key": "min", "value_literal": 1})], "Validator") == []


def test_resolve_components_is_none_until_a_catalog_registers_components() -> None:
    """Base has no components; only registered catalogs contribute, and identical duplicates are tolerated."""
    from facade.catalog_validation import resolve_components

    assert resolve_components([]) is None
    assert resolve_components([models.UICatalog(name="a", operations=[{"name": "x", "arguments": [], "returns": "ANY"}])]) is None

    box = {"name": "Box", "props": [], "accepts_children": True}
    knob = {"name": "Knob", "props": [{"key": "value", "kind": "FLOAT", "required": False, "description": None}], "accepts_children": False, "description": None}
    resolved = resolve_components([models.UICatalog(name="a", components=[box]), models.UICatalog(name="b", components=[box, knob])])
    assert set(resolved) == {"Box", "Knob"}

    with pytest.raises(ValueError, match="component 'Knob' is defined differently by catalogs 'a' and 'b'"):
        resolve_components([models.UICatalog(name="a", components=[knob]), models.UICatalog(name="b", components=[{**knob, "accepts_children": True}])])
