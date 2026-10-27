"""Cross-tenant isolation of the blok mutations.

The queries were scoped by the audit; the mutations were not. A caller in org A must not be able
to update, delete, materialize or re-bind anything that belongs to org B, and a rejected call must
leave no rows behind.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok, BlokAgentMapping, BlokDependency, Dashboard, DashboardPlacement, MaterializedBlok, Module
from facade.schema import schema
from facade.models import Caller
from tests.factories import TEST_TOKEN, create_agent_for_registry
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.graphql_ops import DELETE_BLOK, DELETE_MATERIALIZED_BLOK, MATERIALIZE_BLOK, UPDATE_BLOK, UPDATE_MATERIALIZED_BLOK


@sync_to_async
def _seed_two_tenants(prefix: str) -> dict:
    """Two tenants, each with an agent, a blok with one dependency, a materialization and a dashboard."""
    tenants = {}
    for side, token in (("a", TEST_TOKEN), ("b", OTHER_TOKEN)):
        context, user, client, org = tenant_context(token)
        caller, _ = Caller.objects.get_or_create(client=client, user=user, organization=org)
        agent = create_agent_for_registry(caller, user, org, f"{prefix}-{side}")
        catalog = Module.objects.get_or_create(name="default", organization=org)[0]
        blok = Blok.objects.create(name=f"{prefix}-{side}-blok", creator=user, organization=org, module=catalog)
        BlokDependency.objects.create(blok=blok, key="stage")
        mblok = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
        BlokAgentMapping.objects.create(materialized_blok=mblok, key="stage", dependency=blok.dependencies.get(), agent=agent)
        tenants[side] = {
            "context": context,
            "org": org,
            "agent": agent,
            "blok": blok,
            "mblok": mblok,
            "dashboard": Dashboard.objects.create(name=f"{prefix}-{side}-dash", organization=org),
        }
    return tenants


@sync_to_async
def _snapshot() -> tuple:
    """Everything a rejected mutation must leave untouched."""
    return (
        sorted(Blok.objects.values_list("id", "name")),
        MaterializedBlok.objects.count(),
        sorted(BlokAgentMapping.objects.values_list("materialized_blok_id", "agent_id")),
        DashboardPlacement.objects.count(),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokTenantIsolation:
    async def test_update_blok_cross_tenant_rejected(self, authenticated_context: HttpContext) -> None:
        """Update blok cross tenant rejected."""
        t = await _seed_two_tenants("xt-upd")
        before = await _snapshot()

        result = await schema.execute(UPDATE_BLOK, context_value=t["a"]["context"], variable_values={"input": {"id": str(t["b"]["blok"].id), "name": "hijacked"}})

        assert result.errors, "org A updated org B's blok"
        assert await _snapshot() == before

    async def test_delete_blok_cross_tenant_rejected(self, authenticated_context: HttpContext) -> None:
        """Delete blok cross tenant rejected."""
        t = await _seed_two_tenants("xt-del")
        before = await _snapshot()

        result = await schema.execute(DELETE_BLOK, context_value=t["a"]["context"], variable_values={"input": {"id": str(t["b"]["blok"].id)}})

        assert not result.errors, result.errors
        assert result.data["deleteBlok"] is False
        assert await _snapshot() == before

    async def test_materialized_blok_mutations_cross_tenant_rejected(self, authenticated_context: HttpContext) -> None:
        """Materialized blok mutations cross tenant rejected."""
        t = await _seed_two_tenants("xt-mat")
        before = await _snapshot()

        updated = await schema.execute(
            UPDATE_MATERIALIZED_BLOK,
            context_value=t["a"]["context"],
            variable_values={"input": {"id": str(t["b"]["mblok"].id), "agentMappings": [{"key": "stage", "agent": str(t["a"]["agent"].id)}]}},
        )
        assert updated.errors, "org A re-bound org B's materialized blok"

        deleted = await schema.execute(DELETE_MATERIALIZED_BLOK, context_value=t["a"]["context"], variable_values={"input": {"id": str(t["b"]["mblok"].id)}})
        assert not deleted.errors, deleted.errors
        assert deleted.data["deleteMaterializedBlok"] is False

        assert await _snapshot() == before

    async def test_materialize_foreign_blok_dashboard_or_agent_rejected(self, authenticated_context: HttpContext) -> None:
        """Materialize foreign blok dashboard or agent rejected."""
        t = await _seed_two_tenants("xt-mz")
        before = await _snapshot()
        ctx_a = t["a"]["context"]

        foreign_blok = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=ctx_a,
            variable_values={"input": {"blok": str(t["b"]["blok"].id), "agentMappings": [{"key": "stage", "agent": str(t["a"]["agent"].id)}]}},
        )
        assert foreign_blok.errors, "org A materialized org B's blok"

        foreign_dashboard = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=ctx_a,
            variable_values={
                "input": {
                    "blok": str(t["a"]["blok"].id),
                    "dashboard": str(t["b"]["dashboard"].id),
                    "agentMappings": [{"key": "stage", "agent": str(t["a"]["agent"].id)}],
                }
            },
        )
        assert foreign_dashboard.errors, "org A placed a blok on org B's dashboard"

        foreign_agent = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=ctx_a,
            variable_values={"input": {"blok": str(t["a"]["blok"].id), "agentMappings": [{"key": "stage", "agent": str(t["b"]["agent"].id)}]}},
        )
        assert foreign_agent.errors, "org A bound org B's agent"

        assert await _snapshot() == before
