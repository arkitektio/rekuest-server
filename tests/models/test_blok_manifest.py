"""Coherence of blok manifests: unique ids, resolvable dependency keys and value paths."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels


def _manifest(components: list[dict], *, dependencies: list[str] = (), demo_state: dict | None = None) -> imodels.BlokImplementationInputModel:
    return imodels.BlokImplementationInputModel(
        key="demo",
        components=components,
        dependencies=[{"key": key} for key in dependencies],
        demo_state=demo_state,
    )


def test_duplicate_component_ids_are_rejected_anywhere_in_the_tree() -> None:
    """Duplicate component ids are rejected anywhere in the tree."""
    with pytest.raises(ValidationError, match="duplicate component id 'root'"):
        _manifest([{"id": "root", "component": "Box", "children": [{"id": "child", "component": "Box", "children": [{"id": "root", "component": "Text"}]}]}])


def test_agent_call_must_target_a_declared_dependency() -> None:
    """Agent call must target a declared dependency."""
    prop = {"key": "onClick", "agent_call": {"dependency": "laser", "operation": "fire"}}
    with pytest.raises(ValidationError, match="undeclared dependency 'laser'"):
        _manifest([{"id": "root", "component": "Button", "props": [prop]}], dependencies=["stage"])
    _manifest([{"id": "root", "component": "Button", "props": [prop]}], dependencies=["laser"])


def test_agent_call_nested_in_util_call_argument_is_checked() -> None:
    """Agent call nested in util call argument is checked."""
    prop = {"key": "onClick", "util_call": {"operation": "chain", "arguments": [{"key": "then", "agent_call": {"dependency": "laser", "operation": "fire"}}]}}
    with pytest.raises(ValidationError, match="undeclared dependency 'laser'"):
        _manifest([{"id": "root", "component": "Button", "props": [prop]}], dependencies=[])


def test_value_path_roots_resolve_against_state_declared_values_and_dependencies() -> None:
    """Value path roots resolve against state declared values and dependencies."""
    components = [
        {
            "id": "root",
            "component": "Box",
            "children": [
                {"id": "exposure", "component": "Slider", "props": [{"key": "value", "dynamic_value": {"path": "/exposure/current"}}]},
                {"id": "picker", "component": "Picker", "props": [{"key": "value", "declares_value": "selected_user"}]},
                {"id": "label", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "/selected_user/name"}}]},
                {"id": "stage_pos", "component": "Text", "props": [{"key": "text", "util_call": {"operation": "fmt", "arguments": [{"key": "v", "value_path": "/stage/position"}]}}]},
            ],
        }
    ]
    _manifest(components, dependencies=["stage"], demo_state={"exposure": {"current": 1}})


def test_dotted_paths_resolve_by_their_first_segment() -> None:
    """Dotted paths, as the python bsx parser emits them, resolve by their first segment."""
    components = [{"id": "root", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "self.scope.lasers"}}]}]
    _manifest(components, dependencies=["self"], demo_state={"self": {"scope": {"lasers": ""}}})
    with pytest.raises(ValidationError, match="references 'other'"):
        _manifest([{"id": "root", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "other.scope.lasers"}}]}], demo_state={"self": {}})


def test_unknown_value_path_root_is_rejected() -> None:
    """Unknown value path root is rejected."""
    components = [{"id": "root", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "/nowhere/x"}}]}]
    with pytest.raises(ValidationError, match="references 'nowhere'"):
        _manifest(components, demo_state={"exposure": 1})


def test_without_demo_state_value_path_roots_are_not_checked() -> None:
    """Without demo state value path roots are not checked."""
    components = [{"id": "root", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "/nowhere/x"}}]}]
    _manifest(components, demo_state=None)


def test_duplicate_declared_value_is_rejected() -> None:
    """Duplicate declared value is rejected."""
    components = [
        {"id": "a", "component": "Picker", "props": [{"key": "value", "declares_value": "sel"}]},
        {"id": "b", "component": "Picker", "props": [{"key": "value", "declares_value": "sel"}]},
    ]
    with pytest.raises(ValidationError, match="declared twice"):
        _manifest(components)


def test_prop_needs_a_binding_or_a_declaration_and_at_most_one_binding() -> None:
    """Prop needs a binding or a declaration and at most one binding."""
    with pytest.raises(ValidationError, match="neither bound nor declares"):
        imodels.ComponentPropInputModel(key="x")
    with pytest.raises(ValidationError, match="at most one of"):
        imodels.ComponentPropInputModel(key="x", static_value=1, dynamic_value={"path": "/a"})
    imodels.ComponentPropInputModel(key="x", declares_value="sel")
    imodels.ComponentPropInputModel(key="x", static_value="40x")
