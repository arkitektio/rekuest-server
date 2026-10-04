"""Relational port-matching engine tests.

Exercises ``facade.managers`` against real ArgPort/ReturnPort rows: macro structural matching
(kind/identifier/index/nullable), deep nested matching via the parent chain, the requires/provides
micro-constraints (candidate ``object`` evaluated against ``compiled_jsonpath`` by jsonb_path_match),
the ``force_*`` count constraints, and organization isolation.
"""

from types import SimpleNamespace

from facade.managers import PortDemand

from rekuest_core.inputs.models import ActionDemandInputModel, DescriptorInputModel, PortMatchInputModel, StateDemandInputModel

import pytest

from facade import managers, models
from facade.descriptors import compile_descriptors_to_jsonpath
from rekuest_core.enums import PortKind

from tests.factories import create_action_for_organization, create_registry_bundle


def port_demand_ids(matches, type="args", force_length=None, force_non_nullable_length=None, force_structure_length=None, organization_id=None):
    """Single port demand through the consolidated matcher entry point."""
    demand = PortDemand(kind=type, matches=matches, force_length=force_length, force_non_nullable_length=force_non_nullable_length, force_structure_length=force_structure_length)
    return managers.get_action_ids_by_port_demands([demand], organization_id=organization_id)


def pm(at=None, key=None, kind=None, identifier=None, descriptors=None, nullable=None, children=None):
    """Build a match-shaped object (attribute access is all the manager needs).

    ``descriptors`` is given as a plain ``{key: value}`` dict for brevity and expanded to the
    ``[{key, value}]`` shape the runtime matcher (PortMatchInput.descriptors) consumes; omitting it yields a
    purely structural (PortMatchInput-style) match.
    """
    descriptor_list = [DescriptorInputModel(key=k, value=v) for k, v in (descriptors or {}).items()] or None
    return PortMatchInputModel(at=at, key=key, kind=kind, identifier=identifier, descriptors=descriptor_list, nullable=nullable, children=children)


def action_demand(hash=None, name=None, arg_matches=None, return_matches=None, force_arg_length=None, force_return_length=None, protocols=None):
    return ActionDemandInputModel(
        hash=hash,
        name=name,
        arg_matches=arg_matches,
        return_matches=return_matches,
        force_arg_length=force_arg_length,
        force_return_length=force_return_length,
        protocols=protocols,
    )


def _make_ports(action, specs, PortModel, descriptor_key, parent=None, parent_path=""):
    """Create ArgPort/ReturnPort rows for a list of port specs (recursing into children)."""
    for index, spec in enumerate(specs):
        key = spec.get("key")
        current_path = f"{parent_path}.{key}" if parent_path else (key or "")
        row = PortModel.objects.create(
            action=action,
            parent=parent,
            index=index,
            key=key,
            key_path=current_path,
            kind=spec.get("kind"),
            identifier=spec.get("identifier"),
            compiled_jsonpath=compile_descriptors_to_jsonpath([SimpleNamespace(**d) for d in spec.get(descriptor_key, [])]),
            nullable=bool(spec.get("nullable", False)),
        )
        _make_ports(action, spec.get("children", []), PortModel, descriptor_key, parent=row, parent_path=current_path)


def make_action(org, prefix, args=None, returns=None):
    action = create_action_for_organization(org, prefix, args=args or [], returns=returns or [])
    _make_ports(action, args or [], models.ArgPort, "requires")
    _make_ports(action, returns or [], models.ReturnPort, "provides")
    action.arg_count = len(args or [])
    action.return_count = len(returns or [])
    action.save(update_fields=["arg_count", "return_count"])
    return action


@pytest.fixture
def catalog(db):
    """A small action catalog in two organizations."""
    _, _, org1, _ = create_registry_bundle("matchorg1")
    _, _, org2, _ = create_registry_bundle("matchorg2")

    # A1: structure input that REQUIRES axes == "c"; returns one INT.
    a1 = make_action(
        org1,
        "match-a1",
        args=[{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False, "requires": [{"key": "axes", "operator": "EQUALS", "value": "c"}]}],
        returns=[{"key": "count", "kind": "INT", "nullable": False}],
    )
    # A2: structure input with NO requires (accepts anything) plus a nested DICT tree; nullable input.
    a2 = make_action(
        org1,
        "match-a2",
        args=[
            {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True, "requires": []},
            {
                "key": "options",
                "kind": "DICT",
                "nullable": True,
                "children": [
                    {
                        "key": "advanced",
                        "kind": "DICT",
                        "children": [
                            {"key": "mask", "kind": "STRUCTURE", "identifier": "@mikro/mask"},
                        ],
                    },
                ],
            },
        ],
    )
    # A3: same shape as A1 but in a different organization (isolation check).
    a3 = make_action(
        org2,
        "match-a3",
        args=[{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False, "requires": [{"key": "axes", "operator": "EQUALS", "value": "c"}]}],
        returns=[{"key": "count", "kind": "INT", "nullable": False}],
    )
    return SimpleNamespace(org1=org1, org2=org2, a1=a1, a2=a2, a3=a3)


def test_macro_match_by_kind(catalog):
    ids = port_demand_ids([pm(kind=PortKind.STRUCTURE)], type="args")
    assert set(ids) == {catalog.a1.id, catalog.a2.id, catalog.a3.id}


def test_macro_match_by_identifier(catalog):
    ids = port_demand_ids([pm(identifier="@mikro/image")], type="args")
    assert set(ids) == {catalog.a1.id, catalog.a2.id, catalog.a3.id}


def test_match_by_return_kind(catalog):
    ids = port_demand_ids([pm(kind=PortKind.INT)], type="returns")
    assert set(ids) == {catalog.a1.id, catalog.a3.id}


def test_nullable_macro_match(catalog):
    # Only a2's structure input is nullable.
    ids = port_demand_ids([pm(identifier="@mikro/image", nullable=True)], type="args")
    assert set(ids) == {catalog.a2.id}


def test_micro_constraint_satisfied(catalog):
    # object satisfies a1's requires (axes == "c"); a2 has no requires so it always matches.
    ids = port_demand_ids([pm(identifier="@mikro/image", descriptors={"axes": "c"})], type="args")
    assert set(ids) == {catalog.a1.id, catalog.a2.id, catalog.a3.id}


def test_micro_constraint_rejected(catalog):
    # object violates a1/a3's requires (axes != "c"); a2 (no requires) still matches.
    ids = port_demand_ids([pm(identifier="@mikro/image", descriptors={"axes": "z"})], type="args")
    assert set(ids) == {catalog.a2.id}


def test_nested_match_two_levels_deep(catalog):
    # options(DICT) -> advanced(DICT) -> mask(STRUCTURE @mikro/mask): only a2.
    demand = pm(
        kind=PortKind.DICT,
        children=[
            pm(
                kind=PortKind.DICT,
                children=[
                    pm(kind=PortKind.STRUCTURE, identifier="@mikro/mask"),
                ],
            ),
        ],
    )
    ids = port_demand_ids([demand], type="args")
    assert set(ids) == {catalog.a2.id}


def test_force_length(catalog):
    # a2 has 2 root args; a1/a3 have 1.
    ids = port_demand_ids([pm(kind=PortKind.STRUCTURE)], type="args", force_length=2)
    assert set(ids) == {catalog.a2.id}


def test_force_structure_length(catalog):
    # Root structure ports: a1=1, a3=1, a2=1 (image; the nested mask is not a root).
    ids = port_demand_ids([pm(identifier="@mikro/image")], type="args", force_structure_length=1)
    assert set(ids) == {catalog.a1.id, catalog.a2.id, catalog.a3.id}


def test_force_non_nullable_length(catalog):
    # a1/a3 have 1 non-nullable root arg; a2 has 0 (both roots nullable).
    ids = port_demand_ids([pm(kind=PortKind.STRUCTURE)], type="args", force_non_nullable_length=1)
    assert set(ids) == {catalog.a1.id, catalog.a3.id}


def test_organization_isolation(catalog):
    ids = port_demand_ids([pm(identifier="@mikro/image")], type="args", organization_id=catalog.org1.id)
    assert set(ids) == {catalog.a1.id, catalog.a2.id}
    assert catalog.a3.id not in ids


def test_action_demand_combines_args_and_returns(catalog):
    demand = action_demand(
        arg_matches=[pm(kind=PortKind.STRUCTURE, descriptors={"axes": "c"})],
        return_matches=[pm(kind=PortKind.INT)],
    )
    ids = managers.get_action_ids_by_action_demands([demand], organization_id=catalog.org1.id)[0]
    assert set(ids) == {catalog.a1.id}


def test_action_demand_by_name(catalog):
    ids = managers.get_action_ids_by_action_demands([action_demand(name=catalog.a1.name)])[0]
    assert set(ids) == {catalog.a1.id}
