"""The legacy JSON matchers index children 0-based.

``jsonb -> N`` on an array is 0-based, unlike the 1-based ``WITH ORDINALITY`` index used for the
root ports; the child clause used to add one, so the first child of a demand was compared against
the second stored child and the last stored child could never match.
"""

import pytest
from authentikate.models import Organization

from facade import managers
from facade.models import StateDefinition
from rekuest_core.enums import PortKind
from tests.models.test_action_matching import pm


def test_child_clause_indexes_from_zero() -> None:
    """The generated SQL addresses the first child as element 0."""
    sql, params = managers.build_sql_for_item_recursive(pm(key="parent", children=[pm(key="first"), pm(key="second")]), 0)
    assert "item->'children'->0->>'key'" in sql
    assert "item->'children'->1->>'key'" in sql
    assert params["children_0_0_key"] == "first" and params["children_0_1_key"] == "second"


@pytest.mark.django_db
def test_state_demand_matches_every_child_position() -> None:
    """A two-child demand finds the definition; the same demand with the children swapped does not."""
    org = Organization.objects.create(identifier="matcher-children-org")
    ports = [{"key": "parent", "kind": "MODEL", "nullable": False, "children": [{"key": "first", "kind": "INT", "nullable": False}, {"key": "second", "kind": "STRING", "nullable": False}]}]
    definition = StateDefinition.objects.create(name="two children", hash="matcher-children-hash", ports=ports, description="", organization=org)

    found = managers.get_state_ids_by_demands([pm(key="parent", children=[pm(key="first", kind=PortKind.INT), pm(key="second", kind=PortKind.STRING)])])
    assert definition.id in set(found)

    last_only = managers.get_state_ids_by_demands([pm(key="parent", children=[pm(key="first"), pm(key="second", kind=PortKind.STRING)])])
    assert definition.id in set(last_only)

    swapped = managers.get_state_ids_by_demands([pm(key="parent", children=[pm(key="second"), pm(key="first")])])
    assert definition.id not in set(swapped)
