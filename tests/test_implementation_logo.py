"""An implementation is drawn by an action logo blok its agent declares: the declaration is checked whole."""

import re

import pytest
from pydantic import ValidationError

from facade.inputs import ImplementAgentInputModel
from tests.registered import blok, node

DEFINITION = {"key": "crop", "name": "Crop", "kind": "FUNCTION", "args": [], "returns": []}


def _logo(*shapes: dict, kind: str = "action") -> list[dict]:
    """A logo blok's tree: one ``<Logo>`` holding ``shapes``."""
    return [blok(kind, "logo", node("Logo", *shapes))]


def _path(**props: object) -> dict:
    return node("Path", d="M0 0", **props)


LOGO = {"key": "crop", "vocab": 2, "context": "LOGO", "components": _logo()}
AGENT_LOGO = {"key": "app", "vocab": 2, "context": "LOGO", "components": _logo(kind="agent")}
PANEL = {"key": "panel", "vocab": 2, "components": [blok("agent", "panel", node("Card"))]}


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
    """``app`` is a logo too, but its ``<Blok>`` says kind agent: only kind action draws an action."""
    with pytest.raises(ValidationError, match=f"drawn by the logo '{logo}', which is not an action logo blok this agent declares"):
        _agent(logo, LOGO, PANEL, AGENT_LOGO)


def test_a_logo_blok_sent_as_another_context_is_refused() -> None:
    with pytest.raises(ValidationError, match="sent as a PANEL blok, but its <Blok> says LOGO"):
        _agent(None, {**AGENT_LOGO, "context": "PANEL"})


@pytest.mark.parametrize(
    ("paint", "key"),
    [("#fbbf24", "fill"), ("currentColor", "stroke")],
)
def test_a_logo_paints_only_with_the_pens(paint: str, key: str) -> None:
    with pytest.raises(ValidationError, match=re.escape("[palette.logo_paint]")):
        _agent("crop", {**LOGO, "components": _logo(_path(**{key: paint}))})


def test_pens_references_and_the_task_pass() -> None:
    shapes = [_path(fill="brand-soft"), _path(stroke="ink"), _path(fill="url(#glow)"), _path(fill={"path": "task.message"})]
    _agent("crop", {**LOGO, "components": _logo(*shapes)})


def test_an_action_logo_reads_only_its_task() -> None:
    with pytest.raises(ValidationError, match=re.escape("[roots.not_in_kind]")):
        _agent("crop", {**LOGO, "components": _logo(_path(fill={"path": "self.mode"}))})
