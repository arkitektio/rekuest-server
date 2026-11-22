"""State-schema matching tests.

Guards the state demand path that was previously broken on two fronts: the raw SQL targeted a
non-existent ``facade_stateschema`` table, and the consuming filters referenced a ``state_schema``
field that no longer exists (the FK is ``definition``). These verify ``get_state_ids_by_demands``
resolves real StateDefinition ids by their port descriptors.
"""

from dataclasses import dataclass

import pytest
from authentikate.models import Organization

from facade import managers, models
from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import (
    PortMatchInputModel,
)


def _org(name: str = "test-org-scope") -> Organization:
    """Protocol / StateDefinition / Blok / Dashboard are organization-scoped; any org will do here."""
    return Organization.objects.get_or_create(identifier=name)[0]


def pm(
    at: int | None = None,
    key: str | None = None,
    kind: PortKind | None = None,
    identifier: str | None = None,
    nullable: bool | None = None,
    children: list[PortMatchInputModel] | None = None,
) -> PortMatchInputModel:
    return PortMatchInputModel(
        at=at,
        key=key,
        kind=kind,
        identifier=identifier,
        nullable=nullable,
        children=children,
    )


@dataclass(frozen=True)
class StateDefinitions:
    """A counter (one INT port) and a tracker (one ``@mikro/roi`` structure)."""

    counter: models.StateDefinition
    tracker: models.StateDefinition


@pytest.fixture
def state_defs(db: None) -> StateDefinitions:
    counter = models.StateDefinition.objects.create(
        organization=_org(),
        name="Counter",
        hash="counter-hash",
        description="counter",
        ports=[
            {
                "key": "count",
                "kind": "INT",
                "identifier": None,
                "nullable": False,
                "children": [],
            }
        ],
    )
    tracker = models.StateDefinition.objects.create(
        organization=_org(),
        name="Tracker",
        hash="tracker-hash",
        description="tracker",
        ports=[
            {
                "key": "position",
                "kind": "STRUCTURE",
                "identifier": "@mikro/roi",
                "nullable": False,
                "children": [],
            }
        ],
    )
    return StateDefinitions(counter=counter, tracker=tracker)


def test_state_match_by_kind(state_defs: StateDefinitions) -> None:
    ids = managers.get_state_ids_by_demands([pm(kind=PortKind.INT)])
    assert set(ids) == {state_defs.counter.pk}


def test_state_match_by_identifier(state_defs: StateDefinitions) -> None:
    ids = managers.get_state_ids_by_demands([pm(identifier="@mikro/roi")])
    assert set(ids) == {state_defs.tracker.pk}


def test_state_match_no_results(state_defs: StateDefinitions) -> None:
    ids = managers.get_state_ids_by_demands([pm(identifier="@mikro/nonexistent")])
    assert ids == []
