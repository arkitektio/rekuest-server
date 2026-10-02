"""
Integration tests for the Rekuest API.

This module contains end-to-end tests that verify complete cross-feature workflows
(agent lifecycle, UI components, error handling) by chaining several GraphQL
operations. The reusable operations live in ``tests/graphql_ops.py``.
"""

import pytest
from kante.context import HttpContext

from facade.schema import schema

from tests.graphql_ops import CREATE_BLOK, CREATE_DASHBOARD, DELETE_AGENT, ENSURE_AGENT, GET_AGENT, GET_AGENTS

pytestmark = pytest.mark.usefixtures("fake_takt")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestIntegration:
    """Integration test suite for complete workflows."""

    async def test_agent_lifecycle_workflow(self, authenticated_context: HttpContext):
        """Test complete agent lifecycle from registration to deletion."""

        # Step 1: Register agent
        agent_result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Integration Test Agent"}})

        assert agent_result.data is not None
        agent_id = agent_result.data["ensureAgent"]["id"]
        assert agent_result.data["ensureAgent"]["name"] == "Integration Test Agent"

        # Step 2: Query for the agent
        query_result = await schema.execute(GET_AGENT, context_value=authenticated_context, variable_values={"id": agent_id})

        assert query_result.data is not None
        assert query_result.data["agent"]["name"] == "Integration Test Agent"

        # Step 3: Ensuring the same agent again should be idempotent.
        update_result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Updated Integration Agent"}})

        assert update_result.data is not None
        assert update_result.data["ensureAgent"]["id"] == agent_id  # Same agent
        assert update_result.data["ensureAgent"]["name"] == "Integration Test Agent"

        # Step 4: Delete the agent
        delete_result = await schema.execute(DELETE_AGENT, context_value=authenticated_context, variable_values={"input": {"id": agent_id}})

        assert delete_result.data is not None
        assert delete_result.data["deleteAgent"] == agent_id

        # Step 5: Verify agent is deleted
        final_query_result = await schema.execute(GET_AGENT, context_value=authenticated_context, variable_values={"id": agent_id})

        assert final_query_result.data is None
        assert final_query_result.errors is not None

    async def test_repeated_ensure_collapses_to_single_agent(self, authenticated_context: HttpContext):
        """Repeated ensureAgent under one registry collapses to a single agent."""

        # Step 1: Ensure the agent several times under the same (authenticated) registry.
        ids = set()
        for i in range(3):
            result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": f"Agent attempt {i}"}})

            assert result.data is not None
            ids.add(result.data["ensureAgent"]["id"])

        # All calls resolve to the same agent (one agent per registry).
        assert len(ids) == 1

        # Step 2: The agents listing contains exactly that one agent.
        all_agents_result = await schema.execute(GET_AGENTS, context_value=authenticated_context)

        assert all_agents_result.data is not None
        agents = all_agents_result.data["agents"]
        assert [a["id"] for a in agents] == list(ids)

        # Step 3: Delete the agent.
        delete_result = await schema.execute(DELETE_AGENT, context_value=authenticated_context, variable_values={"input": {"id": ids.pop()}})
        assert delete_result.data is not None

    async def test_dashboard_and_blok_workflow(self, authenticated_context: HttpContext):
        """Test creating UI components workflow."""

        # Step 1: Create a blok
        blok_result = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "Integration Test Blok", "demoState": {}}})

        assert blok_result.data is not None
        blok_id = blok_result.data["createBlok"]["id"]

        # Step 2: Create a dashboard
        dashboard_result = await schema.execute(CREATE_DASHBOARD, context_value=authenticated_context, variable_values={"input": {"name": "Integration Test Dashboard"}})

        assert dashboard_result.data is not None
        dashboard_id = dashboard_result.data["createDashboard"]["id"]

        # Step 3: Query for created resources
        dashboard_query = """
            query GetDashboard($id: ID!) {
                dashboard(id: $id) {
                    id
                    name
                }
            }
        """

        dashboard_query_result = await schema.execute(dashboard_query, context_value=authenticated_context, variable_values={"id": dashboard_id})

        assert dashboard_query_result.data is not None
        assert dashboard_query_result.data["dashboard"]["name"] == "Integration Test Dashboard"

        blok_query = """
            query GetBlok($id: ID!) {
                blok(id: $id) {
                    id
                    name
                    description
                }
            }
        """

        blok_query_result = await schema.execute(blok_query, context_value=authenticated_context, variable_values={"id": blok_id})

        assert blok_query_result.data is not None
        assert blok_query_result.data["blok"]["name"] == "Integration Test Blok"

    async def test_error_handling_workflow(self, authenticated_context: HttpContext):
        """Test proper error handling in various scenarios."""

        # Test 1: Query non-existent agent
        error_result = await schema.execute(GET_AGENT, context_value=authenticated_context, variable_values={"id": "999999"})

        assert error_result.data is None
        assert error_result.errors is not None

        # Test 2: Create agent with invalid data (unknown input field)
        invalid_result = await schema.execute(
            ENSURE_AGENT,
            context_value=authenticated_context,
            variable_values={
                "input": {
                    # Unknown input field is rejected by input coercion
                    "bogusField": "nope",
                }
            },
        )

        assert invalid_result.errors is not None

        # Test 3: Delete non-existent agent
        delete_error_result = await schema.execute(DELETE_AGENT, context_value=authenticated_context, variable_values={"input": {"id": "999999"}})

        assert delete_error_result.errors is not None
