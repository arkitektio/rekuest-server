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

from facade import managers, models
from facade.descriptors import compile_descriptors_to_jsonpath
from facade.managers import PortDemand
from rekuest_core.inputs.models import DescriptorInputModel, PortMatchInputModel
from tests.factories import create_action_for_organization, create_registry_bundle

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
    assert "Seq Scan" not in plan, plan
