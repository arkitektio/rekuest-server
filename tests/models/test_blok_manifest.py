"""Coherence of a v2 blok manifest (blok language v2): one ``<Blok>`` root, unique ids, roots its
kind reads, declared dependencies, locals, queries and records; refused with the vocabulary's code."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs import models as imodels
from tests.registered import blok, local, node, query

SELF = {"key": "self", "action_dependencies": [{"key": "move", "demand": {"arg_matches": [{"key": "to"}]}}], "state_dependencies": [{"key": "arm", "demand": {"matches": [{"key": "mode", "kind": "STRING"}]}}]}


def _blok(children: list[dict], **fields: object) -> imodels.BlokImplementationInputModel:
    """An agent's panel blok: ``children`` (its declarations first) under its ``<Blok>``."""
    return _tree([blok("agent", "panel", *children)], **fields)


def _tree(components: list[dict], **fields: object) -> imodels.BlokImplementationInputModel:
    return imodels.BlokImplementationInputModel.model_validate({"key": "demo", "vocab": 2, "components": components, **fields})


def _text(path: str) -> list[dict]:
    return [node("Text", text={"path": path})]


POSITIONS = query("positions", "mikro", "query { positions { id } }")


def test_a_blok_says_which_language_it_speaks() -> None:
    with pytest.raises(ValidationError, match="vocab"):
        imodels.BlokImplementationInputModel.model_validate({"key": "demo", "components": [blok("agent", "panel", *_text("self.arm.mode"))]})
    with pytest.raises(ValidationError, match="vocab"):
        _blok(_text("self.arm.mode"), vocab=1)


def test_a_tree_is_one_root_with_unique_ids() -> None:
    with pytest.raises(ValidationError, match="one root, not 2"):
        _tree([blok("agent", "panel"), blok("agent", "panel")])
    with pytest.raises(ValidationError, match=r"bsx\.duplicate_id"):
        _blok([node("Card", node("Text", id="root"), id="root")])
    with pytest.raises(ValidationError, match=r"blok\.root"):
        _tree([node("Card")])


def test_an_agent_blok_reads_self_and_calls_its_declared_actions() -> None:
    call = {"key": "onClick", "declares_value": "move", "agent_call": {"dependency": "self", "operation": "move", "arguments": [{"key": "to", "value_literal": 1}]}}
    _blok([{"id": "", "component": "Button", "props": [call, {"key": "disabled", "dynamic_value": {"path": "move.running"}}]}], dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"deps\.unknown_action"):
        _blok([{"id": "", "component": "Button", "props": [{"key": "onClick", "agent_call": {"dependency": "self", "operation": "fly"}}]}], dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"deps\.unknown_state"):
        _blok(_text("self.legs"), dependencies=[SELF])
    with pytest.raises(ValidationError, match=r"roots\.not_in_kind"):
        _blok(_text("deps.robot.arm"), dependencies=[{"key": "robot"}])


def test_a_path_starts_at_a_root_a_local_or_a_record() -> None:
    with pytest.raises(ValidationError, match=r"roots\.unknown"):
        _blok(_text("nowhere.x"))
    _blok([local("zoom", kind="INT", default=1), *_text("zoom")])
    _blok(_text("then"))
    with pytest.raises(ValidationError, match=r"roots\.event_outside_callback"):
        _blok(_text("event.value"))
    with pytest.raises(ValidationError, match=r"query\.unknown"):
        _blok(_text("queries.positions.value"))
    _blok([POSITIONS, *_text("queries.positions.status")])


def test_one_record_is_written_by_one_call() -> None:
    call = {"key": "onClick", "declares_value": "move", "agent_call": {"dependency": "self", "operation": "move", "arguments": [{"key": "to", "value_literal": 1}]}}
    twice = [{"id": "", "component": "Card", "children": [{"id": "", "component": "Button", "props": [call]} for _ in range(2)]}]
    with pytest.raises(ValidationError, match=r"into\.twice"):
        _blok(twice, dependencies=[SELF])


def test_what_a_blok_declares_is_well_formed() -> None:
    with pytest.raises(ValidationError, match=r"'task' is reserved.*\[roots\.reserved\]"):
        _blok([local("task", kind="STRING"), *_text("args.x")])
    with pytest.raises(ValidationError, match=r"is a mutation.*\[query\.read_only\]"):
        _blok([query("q", "mikro", "mutation { a }"), *_text("args.x")])
    with pytest.raises(ValidationError, match=r"neither the address.*\[manifest\.catalog\]"):
        _blok(_text("args.x"), catalogs=["ui"])
    with pytest.raises(ValidationError, match=r"is not a field path.*\[query\.more\]"):
        _blok([query("q", "mikro", "query { a }", more="a b"), *_text("args.x")])
    with pytest.raises(ValidationError, match=r"roots\.unknown"):
        _blok(_text("now"))
    # A local's kind may be left out: it is inferred from the tree, never filled in here.
    assert imodels.BlokLocalInputModel.model_validate({"key": "zoom", "default": 1}).kind is None
    # A default that does not fit its kind is the checker's finding, not a refusal of the port.
    with pytest.raises(ValidationError, match=r"local\.default"):
        _blok([local("zoom", kind="INT", default="1"), *_text("zoom")])


def test_a_blok_says_the_context_it_is_sent_as() -> None:
    with pytest.raises(ValidationError, match="sent as a SCENE blok, but its <Blok> says PANEL"):
        _blok(_text("then"), context="SCENE")
    with pytest.raises(ValidationError, match=r"kind\.placement"):
        _tree([blok("custom", "panel", *_text("then"))])


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


def test_only_a_panel_blok_takes_args_each_keyed_once() -> None:
    with pytest.raises(ValidationError, match="only a panel blok takes args, not a SCENE blok"):
        _tree([blok("agent", "scene", node("Scene"))], args=[IMAGE_ARG], context="SCENE")
    with pytest.raises(ValidationError, match="Blok manifest args: duplicate key 'image'"):
        _blok(_text("args.image"), args=[IMAGE_ARG, IMAGE_ARG])


def test_an_action_blok_names_only_the_actions_dependencies() -> None:
    space = blok("action", "scene", node("Space", node("Placed", dep="crane"), room="lab"))
    action_blok = imodels.ActionBlokInputModel.model_validate({"context": "SCENE", "vocab": 2, "components": [space]})
    with pytest.raises(ValueError, match=r"Placed.dep: 'crane' is not a dependency of the blok .*\[deps.unknown\]"):
        imodels.check_action_blok(action_blok, [imodels.AgentDependencyInputModel(key="robot")])
