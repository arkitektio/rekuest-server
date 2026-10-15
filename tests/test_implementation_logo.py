"""An implementation is drawn by a logo blok its agent declares: the declaration is checked whole."""

import pytest
from pydantic import ValidationError

from facade.inputs import ImplementAgentInputModel

DEFINITION = {"key": "crop", "name": "Crop", "kind": "FUNCTION", "args": [], "returns": []}
LOGO = {"key": "crop", "context": "LOGO", "components": [{"id": "logo", "component": "SVG"}]}
PANEL = {"key": "panel", "components": [{"id": "root", "component": "Box"}]}


def _agent(logo: str | None, *bloks: dict) -> ImplementAgentInputModel:
    return ImplementAgentInputModel(
        name="cropper",
        implementations=[{"interface": "crop", "definition": DEFINITION, "logo": logo}],
        bloks=list(bloks),
    )


def test_an_implementation_names_a_logo_blok_its_agent_declares() -> None:
    agent = _agent("crop", LOGO, PANEL)
    assert agent.implementations is not None and agent.implementations[0].logo == "crop"
    assert _agent(None, PANEL).implementations is not None


@pytest.mark.parametrize("logo", ["nowhere", "panel"])
def test_a_logo_that_is_not_a_declared_logo_blok_is_refused(logo: str) -> None:
    with pytest.raises(ValidationError, match=f"drawn by the logo '{logo}', which is not a logo blok this agent declares"):
        _agent(logo, LOGO, PANEL)
