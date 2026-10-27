"""An implementation is drawn by an action logo blok its agent declares: the declaration is checked whole."""

import re

import pytest
from pydantic import ValidationError

from facade.inputs import ImplementAgentInputModel

DEFINITION = {"key": "crop", "name": "Crop", "kind": "FUNCTION", "args": [], "returns": []}
LOGO = {"key": "crop", "vocab": 2, "context": "ACTION_LOGO", "components": [{"id": "logo", "component": "ActionLogo"}]}
AGENT_LOGO = {"key": "app", "vocab": 2, "context": "LOGO", "components": [{"id": "logo", "component": "AgentLogo"}]}
PANEL = {"key": "panel", "vocab": 2, "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Card"}]}]}


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


@pytest.mark.parametrize(
    ("paint", "key"),
    [("#fbbf24", "fill"), ("currentColor", "stroke")],
)
def test_a_logo_paints_only_with_the_pens(paint: str, key: str) -> None:
    shape = {"id": "p", "component": "Path", "props": [{"key": "d", "static_value": "M0 0"}, {"key": key, "static_value": paint}]}
    logo = {**LOGO, "components": [{"id": "logo", "component": "ActionLogo", "children": [shape]}]}
    with pytest.raises(ValidationError, match=re.escape("[palette.logo_paint]")):
        _agent("crop", logo)


def test_pens_references_and_the_task_pass() -> None:
    def shape(index: int, prop: dict) -> dict:
        return {"id": f"p{index}", "component": "Path", "props": [{"key": "d", "static_value": "M0 0"}, prop]}

    children = [
        shape(0, {"key": "fill", "static_value": "brand-soft"}),
        shape(1, {"key": "stroke", "static_value": "ink"}),
        shape(2, {"key": "fill", "static_value": "url(#glow)"}),
        shape(3, {"key": "fill", "dynamic_value": {"path": "task.message"}}),
    ]
    _agent("crop", {**LOGO, "components": [{"id": "logo", "component": "ActionLogo", "children": children}]})


def test_an_action_logo_reads_only_its_task() -> None:
    shape = {"id": "p", "component": "Path", "props": [{"key": "fill", "dynamic_value": {"path": "self.mode"}}]}
    with pytest.raises(ValidationError, match=re.escape("[roots.not_in_kind]")):
        _agent("crop", {**LOGO, "components": [{"id": "logo", "component": "ActionLogo", "children": [shape]}]})
