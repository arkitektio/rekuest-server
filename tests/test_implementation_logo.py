"""An implementation is drawn by an action logo blok its agent declares: the declaration is checked whole."""

import re

import pytest
from pydantic import ValidationError

from facade.inputs import ImplementAgentInputModel

DEFINITION = {"key": "crop", "name": "Crop", "kind": "FUNCTION", "args": [], "returns": []}
LOGO = {"key": "crop", "context": "ACTION_LOGO", "components": [{"id": "logo", "component": "ActionLogo"}]}
AGENT_LOGO = {"key": "app", "context": "LOGO", "components": [{"id": "logo", "component": "SVG"}]}
PANEL = {"key": "panel", "components": [{"id": "root", "component": "Box"}]}


def _agent(logo: str | None, *bloks: dict) -> ImplementAgentInputModel:
    return ImplementAgentInputModel(
        name="cropper",
        implementations=[{"interface": "crop", "definition": DEFINITION, "logo": logo}],
        bloks=list(bloks),
    )


def test_an_implementation_names_an_action_logo_blok_its_agent_declares() -> None:
    agent = _agent("crop", LOGO, PANEL)
    assert agent.implementations is not None and agent.implementations[0].logo == "crop"
    assert _agent(None, PANEL).implementations is not None


@pytest.mark.parametrize("logo", ["nowhere", "panel", "app"])
def test_a_logo_that_is_not_a_declared_action_logo_blok_is_refused(logo: str) -> None:
    with pytest.raises(ValidationError, match=f"drawn by the logo '{logo}', which is not an action logo blok this agent declares"):
        _agent(logo, LOGO, PANEL, AGENT_LOGO)


def test_no_catalog_component_lives_in_the_action_logo_context() -> None:
    from rekuest_core.inputs.models import CatalogComponentInputModel

    for context in ({"context": "ACTION_LOGO"}, {"children_context": "ACTION_LOGO"}):
        with pytest.raises(ValidationError, match="ACTION_LOGO is a blok context only"):
            CatalogComponentInputModel(name="ActionLogo", **context)


def _shape(paint: str, key: str = "fill") -> dict:
    return {"id": "p", "component": "Path", "props": [{"key": key, "dynamic_value": {"path": paint[1:]}} if paint.startswith("@") else {"key": key, "static_value": paint}]}


@pytest.mark.parametrize(
    ("node", "demo_state", "message"),
    [
        (_shape("#fbbf24"), None, "not '#fbbf24' in 'fill'"),
        (_shape("currentColor", "stroke"), None, "not 'currentColor' in 'stroke'"),
        (_shape("@task.tint"), {"task": {"tint": "red"}}, "not 'red' in 'fill'"),
        (
            {"id": "loop", "component": "foreach", "props": [{"key": "items", "dynamic_value": {"path": "task.lamps"}}, {"key": "let", "declares_value": "lamp"}], "children": [_shape("@lamp.paint")]},
            {"task": {"lamps": [{"paint": "brand"}, {"paint": "#00ff00"}]}},
            "not '#00ff00' in 'fill'",
        ),
    ],
)
def test_an_action_logo_paints_only_with_the_palette(node: dict, demo_state: dict | None, message: str) -> None:
    from facade.catalog_validation import check_logo_palette
    from rekuest_core.inputs.models import ComponentNodeInputModel

    root = ComponentNodeInputModel.model_validate({"id": "logo", "component": "ActionLogo", "children": [node]})
    with pytest.raises(ValueError, match=re.escape(message)):
        check_logo_palette([root], demo_state)


def test_palette_tokens_references_and_unheld_paths_pass() -> None:
    from facade.catalog_validation import check_logo_palette
    from rekuest_core.inputs.models import ComponentNodeInputModel

    children = [_shape("brand-soft"), _shape("ink", "stroke"), _shape("url(#glow)"), _shape("none", "stroke"), _shape("@task.status")]
    root = ComponentNodeInputModel.model_validate({"id": "logo", "component": "ActionLogo", "children": children})
    check_logo_palette([root], {"task": None})
