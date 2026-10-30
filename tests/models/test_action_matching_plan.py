"""The plan of the question every object's action menu asks: which actions take this structure, given what it is.

The descriptor predicate cannot be indexed (the predicate is the row, the object is the
parameter), so what makes the question cheap is how few ports it is evaluated on. With many
actions and few of them typed on the structure asked about, the planner has to start from that
structure's port rows (the ``identifier`` index) and reach the actions from there, never test
every action of the organization for such a port.
"""

from types import SimpleNamespace

import pytest
from django.db import connection
from django.utils import timezone

from facade import managers, models
from facade.descriptors import compile_descriptors_to_jsonpath
from facade.managers import PortDemand
from rekuest_core.inputs.models import DescriptorInputModel, PortMatchInputModel
from tests.factories import create_action_for_organization, create_agent_for_registry, create_registry_bundle

OTHERS = 3000


@pytest.fixture
def large_catalog(db: None) -> SimpleNamespace:
    """One organization with thousands of actions on another structure, and two on the one asked about."""
    _, _, org, _ = create_registry_bundle("planorg")
    template = create_action_for_organization(org, "plan-template", args=[], returns=[])
    fields = [field for field in models.Action._meta.concrete_fields if not field.primary_key]

    def clone(index: int) -> models.Action:
        values = {field.attname: getattr(template, field.attname) for field in fields}
        values.update(key=f"plan-other-{index}", hash=f"plan-hash-{index:064d}"[-64:], arg_count=1)
        return models.Action(**values)

    others = models.Action.objects.bulk_create([clone(index) for index in range(OTHERS)])
    models.ArgPort.objects.bulk_create([models.ArgPort(action=action, index=0, key="table", key_path="table", kind="STRUCTURE", identifier="@mikro/tabledataset") for action in others])

    def constrained(prefix: str, value: str) -> models.Action:
        action = create_action_for_organization(org, prefix, args=[], returns=[])
        path = compile_descriptors_to_jsonpath([SimpleNamespace(key="@mikro/annotation_kind", operator="EQUALS", value=value)])
        models.ArgPort.objects.create(action=action, index=0, key="shape", key_path="shape", kind="STRUCTURE", identifier="@mikro/annotation", compiled_jsonpath=path)
        return action

    with connection.cursor() as cursor:
        cursor.execute("ANALYZE facade_action")
        cursor.execute("ANALYZE facade_argport")
    return SimpleNamespace(org=org, paths=constrained("plan-paths", "PATH"), boxes=constrained("plan-boxes", "RECTANGLE"))


def _demand() -> PortDemand:
    match = PortMatchInputModel(identifier="@mikro/annotation", descriptors=[DescriptorInputModel(key="@mikro/annotation_kind", value="PATH")])
    return PortDemand(kind="args", matches=[match])


def test_actions_taking_a_structure_are_found_from_its_ports(large_catalog: SimpleNamespace) -> None:
    """Right answer, and reached from the identifier's port rows rather than by testing every action."""
    assert managers.get_action_ids_by_port_demands([_demand()], organization_id=large_catalog.org.id) == [large_catalog.paths.id]

    sql, params = managers._port_demand_sql([_demand()], organization_id=large_catalog.org.id)
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "Index Cond: ((identifier)::text = '@mikro/annotation'::text)" in plan, plan
    # The agents a Fits reaches are a handful: what must not be scanned is what grows with the catalog.
    assert "Seq Scan on facade_action" not in plan, plan
    assert "Seq Scan on facade_argport" not in plan, plan


def _live_implementation(catalog: SimpleNamespace, action: models.Action, name: str, hardware: dict) -> models.Implementation:
    user, _, _, registry = create_registry_bundle(name)
    agent = create_agent_for_registry(registry, user, catalog.org, name)
    models.Agent.objects.filter(pk=agent.pk).update(connected=True, last_seen=timezone.now(), hardware=hardware)
    return models.Implementation.objects.create(action=action, agent=agent, interface=name)


def _fit(implementation: models.Implementation) -> models.ImplementationFit:
    return models.ImplementationFit.objects.create(implementation=implementation, port_key="shape", compiled_jsonpath="$agent.memory_mib >= 1000", call={})


def test_an_action_is_hidden_only_when_every_live_agent_refuses(large_catalog: SimpleNamespace) -> None:
    """Fits narrows the same statement, which still starts from the identifier's port rows."""
    paths = large_catalog.paths
    small = _live_implementation(large_catalog, paths, "plan-small", {"memory_mib": 10})
    _fit(small)
    assert managers.get_action_ids_by_port_demands([_demand()], organization_id=large_catalog.org.id) == []

    big = _live_implementation(large_catalog, paths, "plan-big", {"memory_mib": 2000})
    _fit(big)
    assert managers.get_action_ids_by_port_demands([_demand()], organization_id=large_catalog.org.id) == [paths.id]

    big.delete()
    no_hardware = _live_implementation(large_catalog, paths, "plan-unknown", None)
    _fit(no_hardware)
    assert managers.get_action_ids_by_port_demands([_demand()], organization_id=large_catalog.org.id) == []

    _live_implementation(large_catalog, paths, "plan-unlimited", {"memory_mib": 1})
    assert managers.get_action_ids_by_port_demands([_demand()], organization_id=large_catalog.org.id) == [paths.id]

    sql, params = managers._port_demand_sql([_demand()], organization_id=large_catalog.org.id)
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "Index Cond: ((identifier)::text = '@mikro/annotation'::text)" in plan, plan
    assert "Seq Scan on facade_action" not in plan, plan


def test_an_estimate_is_the_fastest_agent_that_fits(large_catalog: SimpleNamespace) -> None:
    paths, boxes = large_catalog.paths, large_catalog.boxes
    path = '($."@mikro/size" / $agent.cpu_count)'
    fast_but_refuses = _live_implementation(large_catalog, paths, "plan-fast", {"cpu_count": 100, "memory_mib": 1})
    models.ImplementationFit.objects.create(implementation=fast_but_refuses, port_key="shape", compiled_jsonpath="$agent.memory_mib >= 1000", call={})
    middle = _live_implementation(large_catalog, paths, "plan-middle", {"cpu_count": 10, "memory_mib": 2000})
    slow = _live_implementation(large_catalog, paths, "plan-slow", {"cpu_count": 2})
    models.Implementation.objects.filter(pk__in=[fast_but_refuses.pk, middle.pk, slow.pk]).update(estimated_runtime_path=path, estimated_runtime_port="shape")

    def seconds(identifier: str = "@mikro/annotation") -> dict[int, float | None]:
        annotated = models.Action.objects.filter(pk__in=[paths.pk, boxes.pk]).annotate(estimated_seconds=managers.estimated_seconds(identifier, {"@mikro/size": 1000}))
        return {action.pk: action.estimated_seconds for action in annotated}

    assert seconds() == {paths.pk: 100.0, boxes.pk: None}
    assert seconds("@mikro/other") == {paths.pk: None, boxes.pk: None}
