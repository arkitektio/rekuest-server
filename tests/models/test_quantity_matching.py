"""Dimension-aware matching for QUANTITY ports.

The canonical pint dimensionality derived at input validation is persisted onto the relational
ArgPort/ReturnPort rows by ``rebuild_relational_ports`` and used by ``facade.managers`` as the
wiring-compatibility key: a demand carrying ``dimension`` only matches quantity ports of the
same physical dimension, while dimension-less demands keep matching on kind alone.
"""

from types import SimpleNamespace

import pytest

from facade import managers
from tests.registered import rebuild_relational_ports
from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import DefinitionInputModel

from tests.factories import create_action_for_organization, create_registry_bundle

VOLT_DIM = "[length] ** 2 * [mass] / [current] / [time] ** 3"
FARAD_DIM = "[current] ** 2 * [time] ** 4 / [length] ** 2 / [mass]"


def qpm(dimension=None, kind=PortKind.QUANTITY):
    """A PortMatchInput-shaped structural demand for a quantity port."""
    return SimpleNamespace(at=None, key=None, kind=kind, identifier=None, descriptors=None, nullable=None, children=None, dimension=dimension)


def port_demand_ids(matches, type="args"):
    """Single port demand through the consolidated matcher entry point."""
    demand = SimpleNamespace(kind=type, matches=matches, force_length=None, force_non_nullable_length=None, force_structure_length=None)
    return managers.get_action_ids_by_port_demands([demand])


def make_quantity_action(org, prefix, arg_unit, return_unit):
    definition = DefinitionInputModel.model_validate(
        {
            "key": prefix,
            "version": "1",
            "name": prefix,
            "kind": "FUNCTION",
            "args": [{"key": "level", "kind": "QUANTITY", "nullable": False, "reference_unit": arg_unit}],
            "returns": [{"key": "reading", "kind": "QUANTITY", "nullable": False, "reference_unit": return_unit}],
        }
    )
    action = create_action_for_organization(
        org,
        prefix,
        args=[i.model_dump() for i in definition.args],
        returns=[i.model_dump() for i in definition.returns],
    )
    rebuild_relational_ports(action, definition)
    return action


@pytest.fixture
def catalog(db):
    """Two quantity actions of different physical dimensions in one organization."""
    _, _, org, _ = create_registry_bundle("qmatch")
    voltage = make_quantity_action(org, "qmatch-voltage", arg_unit="volt", return_unit="mV")
    capacitance = make_quantity_action(org, "qmatch-capacitance", arg_unit="farad", return_unit="pF")
    return SimpleNamespace(org=org, voltage=voltage, capacitance=capacitance)


def test_rebuild_persists_dimension_on_relational_rows(catalog):
    arg = catalog.voltage.arg_ports.get(parent__isnull=True)
    assert arg.kind == PortKind.QUANTITY.value
    assert arg.dimension == VOLT_DIM
    ret = catalog.voltage.return_ports.get(parent__isnull=True)
    assert ret.dimension == VOLT_DIM
    assert catalog.capacitance.arg_ports.get(parent__isnull=True).dimension == FARAD_DIM


def test_demand_with_dimension_matches_only_compatible_action(catalog):
    ids = port_demand_ids([qpm(dimension=VOLT_DIM)], type="args")
    assert set(ids) == {catalog.voltage.id}

    ids = port_demand_ids([qpm(dimension=FARAD_DIM)], type="args")
    assert set(ids) == {catalog.capacitance.id}


def test_dimensionless_demand_matches_all_quantities(catalog):
    ids = port_demand_ids([qpm()], type="args")
    assert set(ids) == {catalog.voltage.id, catalog.capacitance.id}


def test_return_side_dimension_matching(catalog):
    # Both actions' return units are non-reference spellings (mV, pF); the derived
    # canonical dimension is what makes them matchable.
    ids = port_demand_ids([qpm(dimension=VOLT_DIM)], type="returns")
    assert set(ids) == {catalog.voltage.id}


def test_dimension_of_wrong_value_matches_nothing(catalog):
    ids = port_demand_ids([qpm(dimension="[luminosity]")], type="args")
    assert ids == []
