"""Catalog model tests: Protocol, Action and StateDefinition + their constraints."""

import pytest
from django.db import IntegrityError

from authentikate.models import Organization
from facade.models import Action, Protocol, StateDefinition

from tests.factories import create_action_for_organization


def _org(name="catalog-test-org"):
    """Protocol and StateDefinition are organization-scoped; these tests only need *an* org."""
    return Organization.objects.get_or_create(identifier=name)[0]


@pytest.mark.django_db(transaction=True)
class TestCatalogModels:
    """Test suite for the action/protocol/state-definition catalog models."""

    def test_protocol_creation(self):
        """Test creating a Protocol model instance."""
        protocol = Protocol.objects.create(organization=_org(), name="Test Protocol", description="A test protocol for unit testing")

        assert protocol.name == "Test Protocol"
        assert protocol.description == "A test protocol for unit testing"

    def test_protocol_unique_name_constraint(self):
        """Test that Protocol names must be unique."""
        Protocol.objects.create(organization=_org(), name="Unique Protocol", description="First protocol")

        # Attempting to create another protocol with the same name should fail
        with pytest.raises(IntegrityError):
            Protocol.objects.create(organization=_org(), name="Unique Protocol", description="Second protocol with same name")

    def test_action_creation(self):
        """Test creating an Action model instance."""
        org = Organization.objects.create(identifier="test-org-3")

        action = create_action_for_organization(
            organization=org,
            prefix="action-creation",
            name="Test Action",
            description="A test action",
            hash="test-hash-123",
        )

        assert action.name == "Test Action"
        assert action.description == "A test action"
        assert action.hash == "test-hash-123"

    def test_state_schema_creation(self):
        """Test creating a StateDefinition model instance."""
        ports_data = {"input": {"type": "string", "description": "Input port"}, "output": {"type": "string", "description": "Output port"}}

        state_schema = StateDefinition.objects.create(organization=_org(), name="Test State Schema", hash="state-hash-123", ports=ports_data, description="A test state schema")

        assert state_schema.name == "Test State Schema"
        assert state_schema.hash == "state-hash-123"
        assert state_schema.ports == ports_data
        assert state_schema.description == "A test state schema"

    def test_state_schema_unique_hash_constraint(self):
        """Test that StateDefinition hash must be unique."""
        StateDefinition.objects.create(organization=_org(), name="First Schema", hash="unique-hash-456", description="First schema")

        # Attempting to create another schema with the same hash should fail
        with pytest.raises(IntegrityError):
            StateDefinition.objects.create(organization=_org(), name="Second Schema", hash="unique-hash-456", description="Second schema with same hash")

    def test_protocol_action_relationship(self):
        """Test the relationship between Protocol and Action."""
        protocol = Protocol.objects.create(organization=_org(), name="Relationship Test Protocol", description="Testing protocol-action relationship")

        org = Organization.objects.create(identifier="test-org-3")

        # Create multiple actions and associate them with the protocol
        action1 = create_action_for_organization(
            organization=org,
            prefix="protocol-action-1",
            name="Action 1",
            hash="action1-hash",
        )
        action1.protocols.add(protocol)

        action2 = create_action_for_organization(
            organization=org,
            prefix="protocol-action-2",
            name="Action 2",
            hash="action2-hash",
        )
        action2.protocols.add(protocol)

        # Test relationship
        protocol_actions = Action.objects.filter(protocols=protocol)
        assert action1 in protocol_actions
        assert action2 in protocol_actions
        assert protocol_actions.count() == 2
