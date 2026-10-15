"""A blok in an agent's declaration is sent as its components or as its BSX source, exactly one."""

import pytest
from pydantic import ValidationError

from rekuest_core.inputs.models import BlokImplementationInputModel

TREE = [{"id": "root", "component": "Box"}]


def test_a_blok_is_sent_as_its_tree_or_its_source() -> None:
    assert BlokImplementationInputModel(key="panel", components=TREE).components is not None
    assert BlokImplementationInputModel(key="panel", source="<Box />").source == "<Box />"


@pytest.mark.parametrize(
    ("sent", "message"),
    [({"components": TREE, "source": "<Box />"}, "not both"), ({}, "sent as its components or as its BSX source")],
)
def test_a_blok_sent_both_ways_or_neither_is_refused(sent: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BlokImplementationInputModel(key="panel", **sent)
