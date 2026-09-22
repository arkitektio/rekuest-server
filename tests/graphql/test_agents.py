"""GraphQL agent queries and mutations: ensure / query / delete / auth / validation."""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Agent
from facade.schema import schema

from tests.graphql_ops import DELETE_AGENT, ENSURE_AGENT, GET_AGENT, GET_AGENTS

#: ``ENSURE_AGENT`` selects only id/name/connected, and is shared with tests that predate this
#: field. A local operation rather than a widened shared one, so those keep asserting what they
#: were written to assert.
ENSURE_AGENT_WITH_DESCRIPTION = """
    mutation EnsureAgent($input: AgentInput!) {
        ensureAgent(input: $input) {
            id
            name
            description
        }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestGraphQLAgents:
    """GraphQL operations over the Agent type."""

    async def test_agents_query(self, authenticated_context: HttpContext):
        """Test fetching all agents via GraphQL query."""
        result = await schema.execute(GET_AGENTS, context_value=authenticated_context)

        assert result.data is not None
        assert "agents" in result.data
        assert isinstance(result.data["agents"], list)

    async def test_single_agent_query(self, authenticated_context: HttpContext):
        """Test fetching a single agent by ID."""
        create_result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Test Query Agent"}})

        assert create_result.data is not None
        agent_id = create_result.data["ensureAgent"]["id"]

        result = await schema.execute(GET_AGENT, context_value=authenticated_context, variable_values={"id": agent_id})

        assert result.data is not None
        assert "agent" in result.data
        assert result.data["agent"]["id"] == agent_id
        assert result.data["agent"]["name"] == "Test Query Agent"

    async def test_query_with_invalid_id(self, authenticated_context: HttpContext):
        """Test querying with an invalid ID returns appropriate error."""
        result = await schema.execute(
            GET_AGENT,
            context_value=authenticated_context,
            variable_values={"id": "999999"},  # Non-existent ID
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    async def test_hardware_records_query(self, authenticated_context: HttpContext):
        """Test fetching hardware records via GraphQL query."""
        query = """
            query GetHardwareRecords {
                hardwareRecords {
                    id
                    cpuCount
                    cpuVendorName
                    cpuFrequency
                    createdAt
                }
            }
        """

        result = await schema.execute(query, context_value=authenticated_context)

        assert result.data is not None
        assert "hardwareRecords" in result.data
        assert isinstance(result.data["hardwareRecords"], list)

    async def test_ensure_agent_mutation_with_name(self, authenticated_context: HttpContext):
        """Test creating an agent with custom name via ensureAgent mutation."""
        result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Custom Test Agent"}})

        assert result.data is not None, f"Errors: {result.errors}"
        assert "ensureAgent" in result.data

        agent = result.data["ensureAgent"]
        assert agent["name"] == "Custom Test Agent"

    async def test_ensure_agent_mutation_default_name(self, authenticated_context: HttpContext):
        """Test creating an agent with default name generation."""
        result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {}})

        assert result.data is not None, f"Errors: {result.errors}"
        assert "ensureAgent" in result.data

        agent = result.data["ensureAgent"]
        assert agent["name"] is not None  # Should have auto-generated name

    async def test_ensure_agent_mutation_is_idempotent_per_registry(self, authenticated_context: HttpContext):
        """ensureAgent is keyed on the registry, so repeated calls reuse the record."""
        # Create first agent
        result1 = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "First Agent"}})

        assert result1.data is not None
        first_agent_id = result1.data["ensureAgent"]["id"]

        # Ensure again with a different name hint.
        result2 = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Updated Agent"}})

        assert result2.data is not None
        second_agent_id = result2.data["ensureAgent"]["id"]

        # ensureAgent is idempotent and returns the existing record.
        assert first_agent_id == second_agent_id
        assert result2.data["ensureAgent"]["name"] == "First Agent"
        assert await sync_to_async(Agent.objects.count)() == 1

    async def test_ensure_agent_records_and_returns_a_description(self, authenticated_context: HttpContext):
        """A name identifies an agent; a description is what tells two of them apart.

        Worth asserting on the *returned* payload and not just the row: the point of the field
        is that a fleet view can read it.
        """
        result = await schema.execute(
            ENSURE_AGENT_WITH_DESCRIPTION,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Rig 4", "description": "The GPU box in the basement"}},
        )

        assert result.data is not None, f"Errors: {result.errors}"
        assert result.data["ensureAgent"]["description"] == "The GPU box in the basement"

    async def test_ensure_agent_leaves_an_omitted_description_alone(self, authenticated_context: HttpContext):
        """Omitting it means "unchanged", not "cleared".

        ``ensureAgent`` is called on every bootstrap, and most callers pass only a name -- if an
        absent description erased the stored one, the field could never survive a restart.
        """
        first = await schema.execute(
            ENSURE_AGENT_WITH_DESCRIPTION,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Rig 4", "description": "The GPU box in the basement"}},
        )
        assert first.data is not None, f"Errors: {first.errors}"

        second = await schema.execute(ENSURE_AGENT_WITH_DESCRIPTION, context_value=authenticated_context, variable_values={"input": {"name": "Rig 4"}})

        assert second.data is not None, f"Errors: {second.errors}"
        assert second.data["ensureAgent"]["id"] == first.data["ensureAgent"]["id"]
        assert second.data["ensureAgent"]["description"] == "The GPU box in the basement"

    async def test_an_agent_that_never_declared_one_has_no_description(self, authenticated_context: HttpContext):
        """Null, not an empty string: there is nothing to derive a description from.

        Every agent that predates the field reads this way too, which is the honest answer.
        """
        result = await schema.execute(ENSURE_AGENT_WITH_DESCRIPTION, context_value=authenticated_context, variable_values={"input": {"name": "Undescribed"}})

        assert result.data is not None, f"Errors: {result.errors}"
        assert result.data["ensureAgent"]["description"] is None

    async def test_delete_agent_mutation(self, authenticated_context: HttpContext):
        """Test deleting an agent via mutation."""
        # First create an agent
        create_result = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Agent To Delete"}})

        assert create_result.data is not None
        agent_id = create_result.data["ensureAgent"]["id"]

        # Now delete the agent
        delete_result = await schema.execute(DELETE_AGENT, context_value=authenticated_context, variable_values={"input": {"id": agent_id}})

        assert delete_result.data is not None
        assert delete_result.data["deleteAgent"] == agent_id

        # Verify agent is deleted by trying to query it
        query_result = await schema.execute(GET_AGENT, context_value=authenticated_context, variable_values={"id": agent_id})

        assert query_result.data is None
        assert query_result.errors is not None

    async def test_mutation_without_authentication(self):
        """Test that mutations require authentication."""
        # Create a context without authentication
        from kante.context import HttpContext, UniversalRequest
        from strawberry.http.temporal_response import TemporalResponse

        unauthenticated_context = HttpContext(
            request=UniversalRequest(
                _extensions={},
                _client=None,
                _user=None,
                _organization=None,
            ),
            response=TemporalResponse(),
            headers={},
            type="http",
        )

        result = await schema.execute(ENSURE_AGENT, context_value=unauthenticated_context, variable_values={"input": {"name": "unauthorized-agent"}})

        # Should fail due to lack of authentication
        assert result.data is None or result.errors is not None

    async def test_invalid_input_mutation(self, authenticated_context: HttpContext):
        """Test mutation with invalid input data (unknown input field)."""
        result = await schema.execute(
            ENSURE_AGENT,
            context_value=authenticated_context,
            variable_values={
                "input": {
                    # Unknown input field is rejected by input coercion
                    "bogusField": "nope",
                }
            },
        )

        # Should fail due to invalid input field
        assert result.errors is not None
