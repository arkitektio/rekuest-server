"""Dimension-aware matching for QUANTITY ports.

The canonical pint dimensionality derived at input validation is persisted onto the relational
ArgPort/ReturnPort rows by ``rebuild_relational_ports`` and used by ``facade.managers`` as the
wiring-compatibility key: a demand carrying ``dimension`` only matches quantity ports of the
same physical dimension, while dimension-less demands keep matching on kind alone.
"""

from dataclasses import dataclass
from typing import Literal

import pytest
from authentikate.models import Organization

from facade import managers, models
from facade.managers import PortDemand
from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import (
    DefinitionInputModel,
    PortMatchInputModel,
)
from tests.factories import create_action_for_organization, create_registry_bundle
from tests.registered import rebuild_relational_ports

VOLT_DIM = "[length] ** 2 * [mass] / [current] / [time] ** 3"
FARAD_DIM = "[current] ** 2 * [time] ** 4 / [length] ** 2 / [mass]"


def qpm(
    dimension: str | None = None, kind: PortKind = PortKind.QUANTITY
) -> PortMatchInputModel:
    """A PortMatchInput-shaped structural demand for a quantity port."""
    return PortMatchInputModel(kind=kind, dimension=dimension)


def port_demand_ids(
    matches: list[PortMatchInputModel],
    type: Literal["args", "returns"] = "args",
) -> list[int]:
    """Single port demand through the consolidated matcher entry point."""
    demand = PortDemand(kind=type, matches=matches)
    return managers.get_action_ids_by_port_demands([demand])


def make_quantity_action(
    org: Organization, prefix: str, arg_unit: str, return_unit: str
) -> models.Action:
    definition = DefinitionInputModel.model_validate(
        {
            "key": prefix,
            "version": "1",
            "name": prefix,
            "kind": "FUNCTION",
            "args": [
                {
                    "key": "level",
                    "kind": "QUANTITY",
                    "nullable": False,
                    "reference_unit": arg_unit,
                }
            ],
            "returns": [
                {
                    "key": "reading",
                    "kind": "QUANTITY",
                    "nullable": False,
                    "reference_unit": return_unit,
                }
            ],
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


@dataclass(frozen=True)
class Catalog:
    """A volt action and a farad action in one organization."""

    org: Organization
    voltage: models.Action
    capacitance: models.Action


@pytest.fixture
def catalog(db: None) -> Catalog:
    """Two quantity actions of different physical dimensions in one organization."""
    _, _, org, _ = create_registry_bundle("qmatch")
    voltage = make_quantity_action(
        org, "qmatch-voltage", arg_unit="volt", return_unit="mV"
    )
    capacitance = make_quantity_action(
        org, "qmatch-capacitance", arg_unit="farad", return_unit="pF"
    )
    return Catalog(org=org, voltage=voltage, capacitance=capacitance)


def test_rebuild_persists_dimension_on_relational_rows(catalog: Catalog) -> None:
    arg = models.ArgPort.objects.get(action=catalog.voltage, parent__isnull=True)
    assert arg.kind == PortKind.QUANTITY.value
    assert arg.dimension == VOLT_DIM
    ret = models.ReturnPort.objects.get(action=catalog.voltage, parent__isnull=True)
    assert ret.dimension == VOLT_DIM
    assert (
        models.ArgPort.objects.get(
            action=catalog.capacitance, parent__isnull=True
        ).dimension
        == FARAD_DIM
    )


def test_demand_with_dimension_matches_only_compatible_action(catalog: Catalog) -> None:
    ids = port_demand_ids([qpm(dimension=VOLT_DIM)], type="args")
    assert set(ids) == {catalog.voltage.pk}

    ids = port_demand_ids([qpm(dimension=FARAD_DIM)], type="args")
    assert set(ids) == {catalog.capacitance.pk}


def test_dimensionless_demand_matches_all_quantities(catalog: Catalog) -> None:
    ids = port_demand_ids([qpm()], type="args")
    assert set(ids) == {catalog.voltage.pk, catalog.capacitance.pk}


def test_return_side_dimension_matching(catalog: Catalog) -> None:
    # Both actions' return units are non-reference spellings (mV, pF); the derived
    # canonical dimension is what makes them matchable.
    ids = port_demand_ids([qpm(dimension=VOLT_DIM)], type="returns")
    assert set(ids) == {catalog.voltage.pk}


def test_dimension_of_wrong_value_matches_nothing(catalog: Catalog) -> None:
    ids = port_demand_ids([qpm(dimension="[luminosity]")], type="args")
    assert ids == []
