"""A blok in an agent's declaration is sent as its components or as its BSX source, exactly one."""

import pytest
from pydantic import ValidationError

from rekuest_core.enums import ComponentContext
from rekuest_core.inputs.models import BlokImplementationInputModel
from tests.registered import blok, node

TREE = [blok("agent", "panel", node("Card"))]
SOURCE = '<Blok kind="agent" context="panel"><Card /></Blok>'


def test_a_blok_is_sent_as_its_tree_or_its_source() -> None:
    assert (
        BlokImplementationInputModel(key="panel", vocab=2, components=TREE).components
        is not None
    )
    assert (
        BlokImplementationInputModel(key="panel", vocab=2, source=SOURCE).source
        == SOURCE
    )


@pytest.mark.parametrize(
    ("sent", "message"),
    [
        ({"components": TREE, "source": SOURCE}, "not both"),
        ({}, "sent as its components or as its BSX source"),
    ],
)
def test_a_blok_sent_both_ways_or_neither_is_refused(sent: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BlokImplementationInputModel(key="panel", vocab=2, **sent)


def test_a_blok_sent_as_its_source_need_not_say_its_context() -> None:
    """Its `<Blok>` says what draws it, and takt reads it there. A tree sent without one is a panel."""
    scene = '<Blok kind="agent" context="scene"><Scene /></Blok>'
    assert (
        BlokImplementationInputModel(key="scene", vocab=2, source=scene).context is None
    )
    assert (
        BlokImplementationInputModel(key="panel", vocab=2, components=TREE).context
        == ComponentContext.PANEL
    )
