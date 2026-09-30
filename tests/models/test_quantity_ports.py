"""The QUANTITY port kind carries `reference_unit`/`proposed_units`/`dimension` through the pydantic layer.

Ports are persisted as pydantic JSON in ``Action.args``/``Action.returns`` (see
``facade/mutations/implementation.py`` ``model_dump`` write path and ``facade/types/action.py``
read path). These checks mirror that flow: the input models strictly validate quantity metadata
with pint (reference_unit required, dimension derived/canonicalized, proposed_units dimension-
checked, unit fields rejected on other kinds), while the output models stay permissive so JSON
persisted before these rules existed still loads.
"""

import pytest
from pydantic import ValidationError

from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import (
    ArgPortInputModel,
    DefinitionInputModel,
    PortInputModel,
    ReturnPortInputModel,
)
from rekuest_core.objects.models import ArgPortModel, ReturnPortModel

# The canonical form the validator derives for "volt" (deterministic rendering from
# rekuest_core.units — alphabetical terms, NOT pint's cache-order-dependent str()).
VOLT_DIM = "[length] ** 2 * [mass] / [current] / [time] ** 3"
PROPOSED = ["uV", "mV", "V"]


def q(key: str = "v", **overrides) -> dict:
    port = {"key": key, "kind": "QUANTITY", "nullable": False, "reference_unit": "volt"}
    port.update(overrides)
    return port


class TestQuantityValidation:
    def test_dimension_is_derived_when_omitted(self):
        port = ArgPortInputModel.model_validate(q())
        assert port.kind == PortKind.QUANTITY
        assert port.dimension == VOLT_DIM
        # The derived value is what lands in the Action.args JSONField.
        assert port.model_dump()["dimension"] == VOLT_DIM

    def test_dimension_is_verified_and_canonicalized_when_provided(self):
        # An equivalent but non-canonical spelling must validate and be normalized.
        port = ArgPortInputModel.model_validate(q(dimension="[mass] * [length] ** 2 / [time] ** 3 / [current]"))
        assert port.dimension == VOLT_DIM

    def test_canonical_dimension_is_a_fixed_point(self):
        # Persisted dimensions get re-validated on later submissions; the canonical
        # form must survive its own round trip regardless of registry cache state.
        from rekuest_core.units import dimensionality_of

        assert dimensionality_of(VOLT_DIM) == VOLT_DIM
        assert dimensionality_of(dimensionality_of("farad")) == dimensionality_of("farad")

    def test_dimensionless_quantities_are_supported(self):
        port = ArgPortInputModel.model_validate(q(reference_unit="percent", proposed_units=["dimensionless"]))
        assert port.dimension == "dimensionless"
        # The sentinel itself must round-trip (pint cannot parse it natively).
        again = ArgPortInputModel.model_validate(q(reference_unit="percent", dimension="dimensionless"))
        assert again.dimension == "dimensionless"

    def test_reciprocal_dimension_renders_with_unit_numerator(self):
        port = ArgPortInputModel.model_validate(q(reference_unit="hertz"))
        assert port.dimension == "1 / [time]"
        from rekuest_core.units import dimensionality_of

        assert dimensionality_of(port.dimension) == port.dimension

    def test_proposed_units_of_same_dimension_pass(self):
        port = ArgPortInputModel.model_validate(q(proposed_units=PROPOSED))
        assert port.proposed_units == PROPOSED
        assert port.dimension == VOLT_DIM

    @pytest.mark.parametrize("unit", ["mV", "volt", "kV"])
    def test_canonicalization_equivalence(self, unit):
        port = ArgPortInputModel.model_validate(q(reference_unit=unit))
        assert port.dimension == VOLT_DIM

    def test_different_quantities_derive_different_dimensions(self):
        farad = ArgPortInputModel.model_validate(q(reference_unit="farad"))
        assert farad.dimension != VOLT_DIM
        assert "[current] ** 2" in farad.dimension

    def test_nested_quantity_under_list(self):
        port = ArgPortInputModel.model_validate(
            {"key": "voltages", "kind": "LIST", "nullable": False, "children": [q(key="v")]}
        )
        assert port.children[0].dimension == VOLT_DIM

    def test_return_port_derives_dimension(self):
        port = ReturnPortInputModel.model_validate(q(key="reading", proposed_units=PROPOSED))
        assert port.dimension == VOLT_DIM
        dumped = port.model_dump()
        assert dumped["reference_unit"] == "volt"
        assert dumped["proposed_units"] == PROPOSED


class TestQuantityRejections:
    def test_missing_reference_unit(self):
        with pytest.raises(ValidationError, match="must declare a reference_unit"):
            ArgPortInputModel.model_validate(q(reference_unit=None))

    def test_bogus_reference_unit(self):
        with pytest.raises(ValidationError, match="Unknown or unparseable unit 'blorbs'"):
            ArgPortInputModel.model_validate(q(reference_unit="blorbs"))

    def test_proposed_unit_of_wrong_dimension(self):
        with pytest.raises(ValidationError, match="proposed unit 'kg' has dimensionality"):
            ArgPortInputModel.model_validate(q(proposed_units=["mV", "kg"]))

    def test_dimension_inconsistent_with_reference_unit(self):
        with pytest.raises(ValidationError, match="inconsistent with reference_unit"):
            ArgPortInputModel.model_validate(q(dimension="[time]"))

    @pytest.mark.parametrize(
        "kind,extra",
        [
            ("INT", {"reference_unit": "volt"}),
            ("STRUCTURE", {"identifier": "@x/y", "proposed_units": ["mV"]}),
            ("STRING", {"dimension": "[time]"}),
        ],
    )
    def test_unit_fields_rejected_on_non_quantity_ports(self, kind, extra):
        with pytest.raises(ValidationError, match="must not set QUANTITY-only fields"):
            ArgPortInputModel.model_validate({"key": "n", "kind": kind, "nullable": False, **extra})

    def test_quantity_under_list_is_validated_too(self):
        with pytest.raises(ValidationError, match="must declare a reference_unit"):
            ArgPortInputModel.model_validate(
                {"key": "voltages", "kind": "LIST", "nullable": False, "children": [q(reference_unit=None)]}
            )

    def test_list_without_children_raises_clean_error(self):
        with pytest.raises(ValidationError, match="exactly one child"):
            ArgPortInputModel.model_validate({"key": "xs", "kind": "LIST", "nullable": False})
        # Regression: the base model used to lack `children` entirely, so this raised
        # AttributeError from inside the validator instead of a ValidationError.
        with pytest.raises(ValidationError, match="exactly one child"):
            PortInputModel.model_validate({"key": "xs", "kind": "LIST", "nullable": False})


class TestRoundTrip:
    def test_non_quantity_ports_default_unit_fields_to_none(self):
        port = ArgPortInputModel.model_validate({"key": "n", "kind": "INT", "nullable": False})
        dumped = port.model_dump()
        assert dumped["reference_unit"] is None
        assert dumped["proposed_units"] is None
        assert dumped["dimension"] is None

    def test_quantity_output_models_read_back_unit_fields(self):
        common = {
            "key": "v_init",
            "kind": "QUANTITY",
            "nullable": False,
            "effects": None,
            "children": None,
            "reference_unit": "volt",
            "proposed_units": PROPOSED,
            "dimension": VOLT_DIM,
        }

        arg = ArgPortModel.model_validate({**common, "validators": None})
        assert arg.reference_unit == "volt"
        assert arg.proposed_units == PROPOSED
        assert arg.dimension == VOLT_DIM

        ret = ReturnPortModel.model_validate(common)
        assert ret.reference_unit == "volt"
        assert ret.proposed_units == PROPOSED
        assert ret.dimension == VOLT_DIM

    def test_output_models_stay_permissive_for_legacy_json(self):
        # Rows persisted before the pint validation existed may lack the derived dimension
        # (or any unit metadata). Reading them back must not error — strictness is input-only.
        legacy = {
            "key": "v_init",
            "kind": "QUANTITY",
            "nullable": False,
            "effects": None,
            "children": None,
            "validators": None,
            "reference_unit": None,
            "proposed_units": None,
            "dimension": None,
        }
        arg = ArgPortModel.model_validate(legacy)
        assert arg.dimension is None


@pytest.mark.django_db
def test_quantity_definition_persists_and_rehydrates():
    """Full write→read flow: definition model_dump into Action JSON, rebuild relational rows, hydrate back."""
    from tests.registered import rebuild_relational_ports
    from tests.factories import create_action_for_organization, create_registry_bundle

    definition = DefinitionInputModel.model_validate(
        {
            "key": "measure",
            "version": "1",
            "name": "Measure",
            "kind": "FUNCTION",
            "args": [q(key="threshold", proposed_units=["mV", "V"])],
            "returns": [q(key="reading")],
        }
    )
    _, _, org, _ = create_registry_bundle("quantity-int")
    # Mirrors the _create_implementation write path (facade/mutations/implementation.py).
    action = create_action_for_organization(
        org,
        "quantity-int",
        args=[i.model_dump() for i in definition.args],
        returns=[i.model_dump() for i in definition.returns],
    )
    rebuild_relational_ports(action, definition)

    # The JSON blob carries the derived canonical dimension alongside the declared units.
    assert action.args[0]["reference_unit"] == "volt"
    assert action.args[0]["proposed_units"] == ["mV", "V"]
    assert action.args[0]["dimension"] == VOLT_DIM
    assert action.returns[0]["dimension"] == VOLT_DIM

    # Read path: the same hydration facade/types/action.py performs for the GraphQL types.
    hydrated_args = [ArgPortModel(**i) for i in action.args]
    assert hydrated_args[0].dimension == VOLT_DIM
    hydrated_returns = [ReturnPortModel(**i) for i in action.returns]
    assert hydrated_returns[0].reference_unit == "volt"

    # The relational matcher rows exist for the QUANTITY ports.
    assert action.arg_ports.get(parent__isnull=True).kind == PortKind.QUANTITY.value
    assert action.return_ports.get(parent__isnull=True).kind == PortKind.QUANTITY.value
