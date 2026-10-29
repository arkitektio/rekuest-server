"""The plan of a union of port demands: ``{demands: A, OR: {demands: B}}`` and ``anyDemands: [[A], [B]]``.

The OR'd filter is ``id IN (A) OR id IN (B)``. Postgres cannot turn that into a semi-join, so it
runs each uncorrelated demand statement once (a hashed SubPlan) and probes every action of the
organization against both: it costs a scan of the organization's actions. ``anyDemands`` is
``id IN (A UNION B)``, which stays a semi-join driven from each set's port rows.

``REKUEST_BENCH=1 pytest tests/models/test_demand_union_plan.py -s`` also prints what a
keystroke costs at two catalogue sizes: the demands alone, the search alone, both, and the
OR'd union against a hand-written ``UNION`` of the two demand statements.
"""

import json
import os
import statistics
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.models import Q, QuerySet
from django.db.models.expressions import RawSQL

from embeddings import engine
from embeddings.search import hybrid_search
from facade import managers, models
from facade.managers import PortDemand
from rekuest_core.inputs.models import PortMatchInputModel
from tests.factories import create_action_for_organization, create_registry_bundle

#: Actions typed on each structure the demands ask about.
TYPED = 20


def _catalog(others: int) -> SimpleNamespace:
    """One organization: ``others`` actions on a table structure, ``TYPED`` each on an image and an annotation."""
    _, _, org, _ = create_registry_bundle(f"union{others}")
    template = create_action_for_organization(org, f"union-template-{others}", args=[], returns=[])
    fields = [field for field in models.Action._meta.concrete_fields if not field.primary_key]
    vectors = [engine.embed_query(word) for word in ("segment cells", "measure intensity", "export table", "track particles", "denoise image")] if engine.enabled() else [None]

    def clone(index: int, name: str) -> models.Action:
        values = {field.attname: getattr(template, field.attname) for field in fields}
        values.update(key=f"union-{name}-{index}", name=f"{name} {index}", hash=f"union-hash-{name}-{index:040d}"[-64:], arg_count=1, embedding=vectors[index % len(vectors)])
        return models.Action(**values)

    def typed(name: str, identifier: str, count: int) -> list[models.Action]:
        actions = models.Action.objects.bulk_create([clone(index, name) for index in range(count)], batch_size=5000)
        models.ArgPort.objects.bulk_create([models.ArgPort(action=action, index=0, key="input", key_path="input", kind="STRUCTURE", identifier=identifier) for action in actions], batch_size=5000)
        return actions

    typed("table", "@mikro/tabledataset", others)
    images = typed("image", "@mikro/image", TYPED)
    annotations = typed("annotation", "@mikro/annotation", TYPED)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE facade_action")
        cursor.execute("ANALYZE facade_argport")
    return SimpleNamespace(org=org, images={action.id for action in images}, annotations={action.id for action in annotations})


def _takes(identifier: str) -> PortDemand:
    return PortDemand(kind="args", matches=[PortMatchInputModel(identifier=identifier)])


def _subquery(catalog: SimpleNamespace, demand: PortDemand) -> RawSQL:
    return managers.get_port_demand_subquery([demand], organization_id=catalog.org.id)


def _union(catalog: SimpleNamespace, *demands: PortDemand) -> RawSQL:
    """``anyDemands``: one statement, the ``UNION`` of the demand statements."""
    return managers.get_any_port_demand_subquery([[demand] for demand in demands], organization_id=catalog.org.id)


def _actions(catalog: SimpleNamespace) -> QuerySet[models.Action]:
    return models.Action.objects.filter(organization=catalog.org)


def _searched(queryset: QuerySet[models.Action], value: str) -> QuerySet[models.Action]:
    """``ActionFilter.search`` as strawberry_django applies it: the annotated queryset, filtered by its Q."""
    queryset, q = hybrid_search(queryset, "", value, Q(name__icontains=value))
    return queryset.filter(q)


def _plan(queryset: QuerySet[models.Action]) -> str:
    return queryset.explain()


def _milliseconds(queryset: QuerySet[models.Action], runs: int = 7) -> float:
    """Median server-side execution time of the queryset, planning included."""
    sql, params = queryset.values("id").query.sql_with_params()
    times = []
    with connection.cursor() as cursor:
        for _ in range(runs):
            cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", params)
            plan = cursor.fetchone()[0]
            plan = json.loads(plan) if isinstance(plan, str) else plan
            times.append(plan[0]["Planning Time"] + plan[0]["Execution Time"])
    return statistics.median(times)


@pytest.fixture
def catalog(db: None) -> SimpleNamespace:
    return _catalog(3000)


def test_or_of_demands_is_the_union(catalog: SimpleNamespace) -> None:
    image, annotation = _takes("@mikro/image"), _takes("@mikro/annotation")
    either = _actions(catalog).filter(Q(id__in=_subquery(catalog, image)) | Q(id__in=_subquery(catalog, annotation)))
    union = _actions(catalog).filter(id__in=_union(catalog, image, annotation))

    assert set(either.values_list("id", flat=True)) == catalog.images | catalog.annotations
    assert set(union.values_list("id", flat=True)) == catalog.images | catalog.annotations


def test_each_side_of_the_or_starts_from_its_identifier(catalog: SimpleNamespace) -> None:
    image, annotation = _takes("@mikro/image"), _takes("@mikro/annotation")
    plan = _plan(_actions(catalog).filter(Q(id__in=_subquery(catalog, image)) | Q(id__in=_subquery(catalog, annotation))))

    assert "Index Cond: ((identifier)::text = '@mikro/image'::text)" in plan, plan
    assert "Index Cond: ((identifier)::text = '@mikro/annotation'::text)" in plan, plan


def test_any_demands_reaches_the_actions_from_the_port_rows(catalog: SimpleNamespace) -> None:
    """The union is joined to the actions, not probed by a scan of all of them."""
    plan = _plan(_actions(catalog).filter(id__in=_union(catalog, _takes("@mikro/image"), _takes("@mikro/annotation"))))

    assert "Index Cond: ((identifier)::text = '@mikro/image'::text)" in plan, plan
    assert "Index Cond: ((identifier)::text = '@mikro/annotation'::text)" in plan, plan
    assert "Seq Scan on facade_action" not in plan, plan


@pytest.mark.skipif(not os.environ.get("REKUEST_BENCH"), reason="timings: set REKUEST_BENCH=1 and run with -s")
@pytest.mark.parametrize("others", [3000, 30000])
def test_print_keystroke_costs(db: None, others: int) -> None:
    catalog = _catalog(others)
    image, annotation, one_arg = _takes("@mikro/image"), _takes("@mikro/annotation"), PortDemand(kind="args", force_length=1)
    actions = _actions(catalog)

    either = Q(id__in=_subquery(catalog, image)) | Q(id__in=_subquery(catalog, annotation))
    broad_either = Q(id__in=_subquery(catalog, image)) | Q(id__in=_subquery(catalog, one_arg))
    shapes = {
        "org scan, no filter": actions,
        "demands A": actions.filter(id__in=_subquery(catalog, image)),
        "demands A OR B": actions.filter(either),
        "anyDemands [[A], [B]]": actions.filter(id__in=_union(catalog, image, annotation)),
        "broad: A OR len=1": actions.filter(broad_either),
        "broad: anyDemands": actions.filter(id__in=_union(catalog, image, one_arg)),
        "search 'segment'": _searched(actions, "segment"),
        "search + A OR B": _searched(actions.filter(either), "segment"),
        "search + anyDemands": _searched(actions.filter(id__in=_union(catalog, image, annotation)), "segment"),
    }
    print(f"\n{others + 2 * TYPED} actions, median of 7, ms (planning + execution)")
    for name, queryset in shapes.items():
        print(f"  {name:<26} {_milliseconds(queryset):8.2f}")
    print("\nplan of search + A OR B:\n" + _plan(shapes["search + A OR B"]))
