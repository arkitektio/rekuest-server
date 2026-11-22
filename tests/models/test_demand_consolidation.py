"""Consolidated demand matching: N demands, one SQL round trip.

``get_action_ids_by_port_demands`` ANDs all demands' clause groups into a single statement —
provably identical to the old per-demand-query + Python-intersection behavior (every clause is
conjunctive). ``get_action_ids_by_action_demands`` keeps the demands independent (index-aligned
result lists) but folds the N round trips into one ``UNION ALL``. The agent-level "must satisfy
EVERY demand" AND is applied by the caller via one ``Exists()`` per demand, where each demand may
be met by a different implementation.
"""

from dataclasses import dataclass
from typing import Literal

import pytest
from authentikate.models import Organization, User
from django.db.models import Exists, OuterRef
from pytest_django import DjangoAssertNumQueries

from facade import managers, models
from facade.managers import PortDemand
from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import PortMatchInputModel
from tests.factories import create_agent_for_registry, create_registry_bundle
from tests.models.test_action_matching import action_demand, make_action, pm


def _org(name: str = "test-org-scope") -> Organization:
    """Protocol / StateDefinition / Blok / Dashboard are organization-scoped; any org will do here."""
    return Organization.objects.get_or_create(identifier=name)[0]


def pd(
    matches: list[PortMatchInputModel] | None = None,
    kind: Literal["args", "returns"] = "args",
    force_length: int | None = None,
    force_non_nullable_length: int | None = None,
    force_structure_length: int | None = None,
) -> PortDemand:
    """A PortDemandInput-shaped object (attribute access is all the manager needs)."""
    return PortDemand(
        kind=kind,
        matches=matches,
        force_length=force_length,
        force_non_nullable_length=force_non_nullable_length,
        force_structure_length=force_structure_length,
    )


@dataclass(frozen=True)
class Catalog:
    """Three actions with overlapping shapes, the identity they were made under."""

    user: User
    org: Organization
    caller: models.Caller
    a1: models.Action
    a2: models.Action
    a3: models.Action


@pytest.fixture
def catalog(db: None) -> Catalog:
    """Three actions with overlapping arg/return shapes in one organization."""
    user, _, org, caller = create_registry_bundle("consol")

    # a1: one @x/a arg; INT return.
    a1 = make_action(
        org,
        "consol-a1",
        args=[
            {"key": "obj", "kind": "STRUCTURE", "identifier": "@x/a", "nullable": False}
        ],
        returns=[{"key": "count", "kind": "INT", "nullable": False}],
    )
    # a2: @x/a arg plus an extra INT arg; @x/b return.
    a2 = make_action(
        org,
        "consol-a2",
        args=[
            {
                "key": "obj",
                "kind": "STRUCTURE",
                "identifier": "@x/a",
                "nullable": False,
            },
            {"key": "extra", "kind": "INT", "nullable": False},
        ],
        returns=[
            {"key": "out", "kind": "STRUCTURE", "identifier": "@x/b", "nullable": False}
        ],
    )
    # a3: one INT arg; @x/b return.
    a3 = make_action(
        org,
        "consol-a3",
        args=[{"key": "n", "kind": "INT", "nullable": False}],
        returns=[
            {"key": "out", "kind": "STRUCTURE", "identifier": "@x/b", "nullable": False}
        ],
    )
    return Catalog(user=user, org=org, caller=caller, a1=a1, a2=a2, a3=a3)


ARGS_XA = [pm(identifier="@x/a")]
RETURNS_XB = [pm(identifier="@x/b")]


def test_multi_demand_equals_per_demand_intersection(catalog: Catalog) -> None:
    # Demand A (args @x/a) alone -> {a1, a2}; demand B (returns @x/b) alone -> {a2, a3}.
    a_ids = set(managers.get_action_ids_by_port_demands([pd(ARGS_XA)]))
    b_ids = set(
        managers.get_action_ids_by_port_demands([pd(RETURNS_XB, kind="returns")])
    )
    assert a_ids == {catalog.a1.pk, catalog.a2.pk}
    assert b_ids == {catalog.a2.pk, catalog.a3.pk}

    combined = set(
        managers.get_action_ids_by_port_demands(
            [pd(ARGS_XA), pd(RETURNS_XB, kind="returns")]
        )
    )
    assert combined == a_ids & b_ids == {catalog.a2.pk}


def test_multi_demand_is_one_query(
    catalog: Catalog, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    with django_assert_num_queries(1):
        ids = managers.get_action_ids_by_port_demands(
            [
                pd(ARGS_XA),
                pd(RETURNS_XB, kind="returns"),
                pd([pm(kind=PortKind.STRUCTURE)]),
            ]
        )
    assert set(ids) == {catalog.a2.pk}


def test_per_demand_force_params_combine(catalog: Catalog) -> None:
    # ARGS force_length=1 -> {a1, a3}; RETURNS @x/b -> {a2, a3}; ANDed -> {a3}.
    ids = managers.get_action_ids_by_port_demands(
        [pd(force_length=1), pd(RETURNS_XB, kind="returns")]
    )
    assert set(ids) == {catalog.a3.pk}


def test_per_demand_count_subqueries_combine(catalog: Catalog) -> None:
    # Non-nullable root args = 1 -> {a1, a3}; root arg structures = 1 -> {a1, a2}; ANDed -> {a1}.
    ids = managers.get_action_ids_by_port_demands(
        [pd(force_non_nullable_length=1), pd(force_structure_length=1)]
    )
    assert set(ids) == {catalog.a1.pk}


def test_all_empty_demands_raise(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="No search params provided"):
        managers.get_action_ids_by_port_demands([pd()])


def test_action_demands_are_index_aligned_in_one_query(
    catalog: Catalog, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    d1 = action_demand(arg_matches=ARGS_XA)
    d2 = action_demand(return_matches=RETURNS_XB)
    d3 = action_demand(name="no-such-action")

    with django_assert_num_queries(1):
        per_demand = managers.get_action_ids_by_action_demands([d1, d2, d3])

    assert [set(ids) for ids in per_demand] == [
        {catalog.a1.pk, catalog.a2.pk},
        {catalog.a2.pk, catalog.a3.pk},
        set(),
    ]


def test_action_demands_single_demand_matches_legacy_shape(catalog: Catalog) -> None:
    # The single-demand callers (queries/logic/filters) use [demand] + [0].
    ids = managers.get_action_ids_by_action_demands(
        [action_demand(name=catalog.a1.name)]
    )[0]
    assert set(ids) == {catalog.a1.pk}


def test_action_demand_protocols_are_matched(catalog: Catalog) -> None:
    # a1 implements "predicate"; a2/a3 don't. Protocols on a demand AND with the port matches
    # (previously the field was accepted on dependency inputs but silently ignored).
    predicate = models.Protocol.objects.create(
        name="predicate", description="single bool return", organization=_org()
    )
    catalog.a1.protocols.add(predicate)

    ids = managers.get_action_ids_by_action_demands(
        [action_demand(arg_matches=ARGS_XA, protocols=["predicate"])]
    )[0]
    assert set(ids) == {catalog.a1.pk}

    ids = managers.get_action_ids_by_action_demands(
        [
            action_demand(
                arg_matches=ARGS_XA, protocols=["predicate", "no-such-protocol"]
            )
        ]
    )[0]
    assert ids == []


def test_action_demand_identity_pins_and_matches_loosen(catalog: Catalog) -> None:
    """app/key/version on the demand pin a specific action ("imagej/open_image"); dropping
    them and relying on the structural matches loosens the demand to equivalent actions of
    other apps."""
    # Two structurally equivalent actions provided by different apps.
    imagej = make_action(
        catalog.org,
        "consol-imagej",
        args=[
            {
                "key": "img",
                "kind": "STRUCTURE",
                "identifier": "@y/img",
                "nullable": False,
            }
        ],
    )
    fiji = make_action(
        catalog.org,
        "consol-fiji",
        args=[
            {
                "key": "img",
                "kind": "STRUCTURE",
                "identifier": "@y/img",
                "nullable": False,
            }
        ],
    )

    # Pinned by app + key: only the imagej action (factory: key=<prefix>-key, app=<prefix>-app).
    pinned = action_demand(arg_matches=None)
    pinned.key, pinned.app, pinned.version = (
        "consol-imagej-key",
        "consol-imagej-app",
        None,
    )
    ids = managers.get_action_ids_by_action_demands([pinned])[0]
    assert set(ids) == {imagej.pk}

    # Loosened to the structural matches only: both apps' equivalents match.
    loose = action_demand(arg_matches=[pm(identifier="@y/img")])
    ids = managers.get_action_ids_by_action_demands([loose])[0]
    assert set(ids) == {imagej.pk, fiji.pk}

    # Version is part of the identity (factory registers 1.0.0).
    versioned = action_demand(arg_matches=[pm(identifier="@y/img")])
    versioned.version = "2.0.0"
    ids = managers.get_action_ids_by_action_demands([versioned])[0]
    assert ids == []


def test_action_demand_qualifiers_are_matched(catalog: Catalog) -> None:
    """Tri-state semantic qualifiers on the demand: True/False filter, None matches either."""
    models.Action.objects.filter(pk=catalog.a1.pk).update(pure=True, idempotent=True)

    pure_demand = action_demand(arg_matches=ARGS_XA)
    pure_demand.pure = True
    ids = managers.get_action_ids_by_action_demands([pure_demand])[0]
    assert set(ids) == {catalog.a1.pk}

    impure_demand = action_demand(arg_matches=ARGS_XA)
    impure_demand.pure = False
    ids = managers.get_action_ids_by_action_demands([impure_demand])[0]
    assert set(ids) == {catalog.a2.pk}

    # None (the helper default) matches either.
    ids = managers.get_action_ids_by_action_demands(
        [action_demand(arg_matches=ARGS_XA)]
    )[0]
    assert set(ids) == {catalog.a1.pk, catalog.a2.pk}


def test_agent_must_satisfy_every_demand(catalog: Catalog) -> None:
    """Mirrors AgentFilter.action_demands: one Exists() per demand, ANDed at the agent level.

    Each demand may be satisfied by a DIFFERENT implementation of the agent — an agent
    implementing a1 (demand 1) and a3 (demand 2) matches, one implementing only a1 does not.
    """
    # Agents are unique per (client, user, organization) — each needs its own registry.
    agent_both = create_agent_for_registry(
        catalog.caller, catalog.user, catalog.org, "consol-both"
    )
    partial_user, _, _, partial_caller = create_registry_bundle("consol-partial")
    agent_partial = create_agent_for_registry(
        partial_caller, partial_user, catalog.org, "consol-partial"
    )
    for prefix, agent, action in [
        ("both-1", agent_both, catalog.a1),
        ("both-2", agent_both, catalog.a3),
        ("partial-1", agent_partial, catalog.a1),
    ]:
        models.Implementation.objects.create(
            interface=f"{prefix}-iface", action=action, agent=agent, needs_token=False
        )

    demands = [
        action_demand(arg_matches=ARGS_XA),
        action_demand(return_matches=RETURNS_XB),
    ]
    per_demand_ids = managers.get_action_ids_by_action_demands(
        demands, organization_id=catalog.org.pk
    )

    queryset = models.Agent.objects.all()
    for new_ids in per_demand_ids:
        queryset = queryset.filter(
            Exists(
                models.Implementation.objects.filter(
                    agent=OuterRef("pk"), action_id__in=new_ids
                )
            )
        )

    assert set(queryset.values_list("id", flat=True)) == {agent_both.pk}
