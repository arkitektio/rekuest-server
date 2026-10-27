"""Coherence of a v2 blok manifest (blok language v2): one root, unique ids, roots its kind
reads, declared dependencies, locals, queries and records; refused with the vocabulary's code."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels

SELF = {"key": "self", "action_dependencies": [{"key": "move", "demand": {"arg_matches": [{"key": "to"}]}}], "state_dependencies": [{"key": "arm", "demand": {"matches": [{"key": "mode", "kind": "STRING"}]}}]}


def _blok(components: list[dict], **fields: object) -> imodels.BlokImplementationInputModel:
    """An html blok's single root is wrapped in its `Html` root (the root names the context)."""
    if fields.get("context", "HTML") == "HTML" and len(components) == 1:
        components = [{"id": "Html[0]", "component": "Html", "children": components}]
    return imodels.BlokImplementationInputModel.model_validate({"key": "demo", "vocab": 2, "components": components, **fields})


def _text(path: str) -> list[dict]:
    return [{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": path}}]}]


def test_a_blok_says_which_language_it_speaks() -> None:
    with pytest.raises(ValidationError, match="vocab"):
        imodels.BlokImplementationInputModel.model_validate({"key": "demo", "components": [{"id": "Html[0]", "component": "Html", "children": _text("self.arm.mode")}]})
    with pytest.raises(ValidationError, match="vocab"):
        _blok(_text("self.arm.mode"), vocab=1)


def test_a_tree_is_one_root_with_unique_ids() -> None:
    with pytest.raises(ValidationError, match="one root, not 2"):
        _blok([{"id": "a", "component": "Card"}, {"id": "b", "component": "Card"}])
    with pytest.raises(ValidationError, match=r"bsx\.duplicate_id"):
        _blok([{"id": "root", "component": "Card", "children": [{"id": "root", "component": "Text"}]}])


def test_an_agent_blok_reads_self_and_calls_its_declared_actions() -> None:
    call = {"key": "onClick", "declares_value": "move", "agent_call": {"dependency": "self", "operation": "move", "arguments": [{"key": "to", "value_literal": 1}]}}
    _blok([{"id": "Button[0]", "component": "Button", "props": [call, {"key": "disabled", "dynamic_value": {"path": "move.running"}}]}], dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"deps\.unknown_action"):
        _blok([{"id": "Button[0]", "component": "Button", "props": [{"key": "onClick", "agent_call": {"dependency": "self", "operation": "fly"}}]}], dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"deps\.unknown_state"):
        _blok(_text("self.legs"), dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"roots\.not_in_kind"):
        _blok(_text("deps.robot.arm"), dependencies=[{"key": "robot"}])


def test_a_path_starts_at_a_root_a_local_or_a_record() -> None:
    with pytest.raises(ValidationError, match=r"roots\.unknown"):
        _blok(_text("nowhere.x"))
    _blok(_text("zoom"), locals=[{"key": "zoom", "kind": "INT", "default": 1}])
    _blok(_text("then"))
    with pytest.raises(ValidationError, match=r"roots\.event_outside_callback"):
        _blok(_text("event.value"))
    with pytest.raises(ValidationError, match=r"query\.unknown"):
        _blok(_text("queries.positions.value"))
    _blok(_text("queries.positions.status"), queries=[{"key": "positions", "service": "mikro", "document": "query { positions { id } }"}])


def test_one_record_is_written_by_one_call() -> None:
    call = {"key": "onClick", "declares_value": "move", "agent_call": {"dependency": "self", "operation": "move", "arguments": [{"key": "to", "value_literal": 1}]}}
    twice = [{"id": "Card[0]", "component": "Card", "children": [{"id": f"b{i}", "component": "Button", "props": [call]} for i in range(2)]}]
    with pytest.raises(ValidationError, match=r"into\.twice"):
        _blok(twice, dependencies=[SELF])


def test_what_a_blok_declares_beside_its_tree_is_well_formed() -> None:
    with pytest.raises(ValidationError, match=r"'task' is reserved.*\[roots\.reserved\]"):
        _blok(_text("args.x"), locals=[{"key": "task", "kind": "STRING"}])
    with pytest.raises(ValidationError, match=r"locals: 'zoom' is declared twice.*\[manifest\.duplicate\]"):
        _blok(_text("args.x"), locals=[{"key": "zoom", "kind": "INT"}, {"key": "zoom", "kind": "INT"}])
    with pytest.raises(ValidationError, match=r"is a mutation.*\[query\.read_only\]"):
        _blok(_text("args.x"), queries=[{"key": "q", "service": "mikro", "document": "mutation { a }"}])
    with pytest.raises(ValidationError, match=r"not name@version.*\[manifest\.catalog\]"):
        _blok(_text("args.x"), catalogs=["ui"])
    with pytest.raises(ValidationError, match=r"is not a field path.*\[query\.more\]"):
        _blok(_text("args.x"), queries=[{"key": "q", "service": "mikro", "document": "query { a }", "more": "a b"}])
    with pytest.raises(ValidationError, match=r"roots\.unknown"):
        _blok(_text("now"))
    # A local's kind may be left out: it is inferred from the tree, never filled in here.
    assert imodels.BlokLocalInputModel.model_validate({"key": "zoom", "default": 1}).kind is None
    # A default that does not fit its kind is the checker's finding, not a refusal of the port.
    with pytest.raises(ValidationError, match=r"local\.default"):
        _blok(_text("zoom"), locals=[{"key": "zoom", "kind": "INT", "default": "1"}])


def test_a_prop_is_bound_at_most_one_way_and_unbound_is_null() -> None:
    with pytest.raises(ValidationError, match="at most one of"):
        imodels.ComponentPropInputModel(key="x", static_value=1, dynamic_value={"path": "a"})
    assert imodels.ComponentPropInputModel(key="x").static_value is None
    # A literal is kept as written: a bool stays a bool, a list a list.
    assert imodels.ComponentPropInputModel(key="persist", static_value=True).static_value is True
    assert imodels.ComponentPropInputModel(key="through", static_value=["robot"]).static_value == ["robot"]


def test_a_widget_prop_is_held_to_base_v1() -> None:
    """A widget feeds its definition's hash: its props keep the rules they had before v2."""
    widget = imodels.CustomAssignWidgetInputModel(kind="CUSTOM", component="Knob", props=[{"key": "on", "static_value": True}])
    assert widget.props is not None and widget.props[0].static_value == 1
    with pytest.raises(ValidationError, match="neither bound nor declares"):
        imodels.CustomAssignWidgetInputModel(kind="CUSTOM", component="Knob", props=[{"key": "on"}])


IMAGE_ARG = {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}


def test_a_blok_reads_the_args_it_declares() -> None:
    blok = _blok(_text("args.image"), args=[IMAGE_ARG])
    assert blok.args[0].identifier == "@mikro/image"
    with pytest.raises(ValidationError, match=r"args\.unknown"):
        _blok(_text("args.other"), args=[IMAGE_ARG])


def test_only_an_html_blok_takes_args_each_keyed_once() -> None:
    with pytest.raises(ValidationError, match="only an html blok takes args, not a SCENE blok"):
        _blok([{"id": "Scene[0]", "component": "Scene"}], args=[IMAGE_ARG], context="SCENE")
    with pytest.raises(ValidationError, match="Blok manifest args: duplicate key 'image'"):
        _blok(_text("args.image"), args=[IMAGE_ARG, IMAGE_ARG])


def test_an_action_blok_names_only_the_actions_dependencies() -> None:
    blok = imodels.ActionBlokInputModel.model_validate({"context": "OVERLAY", "vocab": 2, "components": [
        {"id": "root", "component": "Overlay", "children": [{"id": "a", "component": "Anchor", "props": [{"key": "dep", "static_value": "crane"}]}]}]})
    with pytest.raises(ValueError, match="names 'crane', which is not a dependency of the action"):
        imodels.check_action_blok(blok, [imodels.AgentDependencyInputModel(key="robot")])
