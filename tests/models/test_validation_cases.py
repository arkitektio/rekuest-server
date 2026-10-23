"""The blok validation cases rekuest and takt must agree on (``rekuest_core/catalogs/validation_cases.json``).

takt runs the same file in ``crates/facade/tests``: a rule changed on one side only fails on the other.
"""

import json
import re
from pathlib import Path

import pytest

from facade import models
from facade.catalog_validation import validate_components_against_catalogs
from rekuest_core import enums
from rekuest_core.inputs import models as rimodels

CASES = json.loads((Path(__file__).parents[2] / "rekuest_core" / "catalogs" / "validation_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_validation_case(case: dict) -> None:
    catalogs = [
        models.UICatalog(name=catalog["name"], components=catalog["components"], operations=catalog["operations"])
        for catalog in case["catalogs"]
    ]
    components = [rimodels.ComponentNodeInputModel.model_validate(node) for node in case["components"]]
    context = enums.ComponentContext(case.get("context", "HTML"))
    expect = case["expect"]

    if expect["refused"] is not None:
        with pytest.raises(ValueError, match=re.escape(expect["refused"])):
            validate_components_against_catalogs(catalogs, components, context, case.get("demo_state"))
        return
    diagnostics = validate_components_against_catalogs(catalogs, components, context, case.get("demo_state"))
    assert [diagnostic.code for diagnostic in diagnostics] == expect["diagnostics"]
