"""Agent model tests: creation, defaults, identity and constraints."""

import pytest
from django.db import IntegrityError

from facade.models import Agent

from tests.factories import create_agent_for_registry, create_registry_bundle


@pytest.mark.django_db(transaction=True)
class TestAgentModels:
    """Test suite for the Agent model."""

    def test_agent_creation(self):
        """Test creating an Agent model instance."""
        user, client, org, caller = create_registry_bundle("agent-creation")

        agent = create_agent_for_registry(
            registry=caller,
            user=user,
            organization=org,
            prefix="agent-creation",
            name="Test Agent",
        )

        assert agent.name == "Test Agent"
        # The agent now carries its own (client, user, organization) identity directly.
        assert agent.client == client
        assert agent.user == user
        assert agent.organization == org

    def test_agent_default_values(self):
        """Test Agent model default values."""
        user, _, org, caller = create_registry_bundle("agent-defaults")

        agent = create_agent_for_registry(
            registry=caller,
            user=user,
            organization=org,
            prefix="agent-defaults",
        )

        assert agent.name == "Nana"  # Default name

    def test_agent_str_representation(self):
        """Test Agent model string representation."""
        user, _, org, caller = create_registry_bundle("agent-str")

        agent = create_agent_for_registry(
            registry=caller,
            user=user,
            organization=org,
            prefix="agent-str",
            name="Test Agent Display",
        )

        assert "Test Agent Display" in str(agent)

    def test_identity_independence(self):
        """Agents in different (client, user, organization) identities are independent."""
        user1, client1, org1, caller1 = create_registry_bundle("identity-1")
        user2, client2, org2, caller2 = create_registry_bundle("identity-2")

        agent1 = create_agent_for_registry(registry=caller1, user=user1, organization=org1, prefix="identity-1", name="Agent 1")
        agent2 = create_agent_for_registry(registry=caller2, user=user2, organization=org2, prefix="identity-2", name="Agent 2")

        assert list(Agent.objects.filter(client=client1)) == [agent1]
        assert list(Agent.objects.filter(client=client2)) == [agent2]

    def test_agent_cascade_delete(self):
        """Deleting the client cascades to its agents (Agent.client is CASCADE)."""
        user, client, org, caller = create_registry_bundle("agent-cascade")

        agent = create_agent_for_registry(
            registry=caller,
            user=user,
            organization=org,
            prefix="agent-cascade",
            name="To Be Deleted",
        )
        agent_pk = agent.pk

        client.delete()

        assert not Agent.objects.filter(pk=agent_pk).exists()

    def test_one_agent_per_identity(self):
        """Only one Agent is allowed per (client, user, organization)."""
        user, _, org, caller = create_registry_bundle("meta")

        create_agent_for_registry(registry=caller, user=user, organization=org, prefix="meta-1")

        # A second agent with the same client/user/organization must fail the unique constraint.
        with pytest.raises(IntegrityError):
            create_agent_for_registry(registry=caller, user=user, organization=org, prefix="meta-2")
