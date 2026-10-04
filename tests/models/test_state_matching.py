"""State-schema matching tests.

Guards the state demand path that was previously broken on two fronts: the raw SQL targeted a
non-existent ``facade_stateschema`` table, and the consuming filters referenced a ``state_schema``
field that no longer exists (the FK is ``definition``). These verify ``get_state_ids_by_demands``
resolves real StateDefinition ids by their port descriptors.
"""

from types import SimpleNamespace

from rekuest_core.inputs.models import ActionDemandInputModel, DescriptorInputModel, PortMatchInputModel, StateDemandInputModel

import pytest

from facade import managers, models
from rekuest_core.enums import PortKind


def _org(slug="test-org-scope"):
    """Protocol / StateDefinition / Blok / Dashboard are organization-scoped; any org will do here."""
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug=slug)[0]


def pm(at=None, key=None, kind=None, identifier=None, nullable=None, children=None):
    return PortMatchInputModel(at=at, key=key, kind=kind, identifier=identifier, nullable=nullable, children=children)


@pytest.fixture
def state_defs(db):
    counter = models.StateDefinition.objects.create(
        organization=_org(),
        name="Counter",
        hash="counter-hash",
        description="counter",
        ports=[{"key": "count", "kind": "INT", "identifier": None, "nullable": False, "children": []}],
    )
    tracker = models.StateDefinition.objects.create(
        organization=_org(),
        name="Tracker",
        hash="tracker-hash",
        description="tracker",
        ports=[{"key": "position", "kind": "STRUCTURE", "identifier": "@mikro/roi", "nullable": False, "children": []}],
    )
    return SimpleNamespace(counter=counter, tracker=tracker)


def test_state_match_by_kind(state_defs):
    ids = managers.get_state_ids_by_demands([pm(kind=PortKind.INT)])
    assert set(ids) == {state_defs.counter.id}


def test_state_match_by_identifier(state_defs):
    ids = managers.get_state_ids_by_demands([pm(identifier="@mikro/roi")])
    assert set(ids) == {state_defs.tracker.id}


def test_state_match_no_results(state_defs):
    ids = managers.get_state_ids_by_demands([pm(identifier="@mikro/nonexistent")])
    assert ids == []
