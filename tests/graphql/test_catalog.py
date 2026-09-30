"""GraphQL catalog queries: actions, protocols, state schemas and clients."""

import pytest
from kante.context import HttpContext

from facade.schema import schema


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestGraphQLCatalog:
    """Read-side GraphQL operations over the action/protocol/state catalog."""

    async def test_actions_query(self, authenticated_context: HttpContext):
        """Test fetching all actions via GraphQL query."""
        query = """
            query GetActions {
                actions {
                    id
                    name
                    description
                    hash
                }
            }
        """

        result = await schema.execute(query, context_value=authenticated_context)

        assert result.data is not None
        assert "actions" in result.data
        assert isinstance(result.data["actions"], list)

    async def test_protocols_query(self, authenticated_context: HttpContext):
        """Test fetching all protocols via GraphQL query."""
        query = """
            query GetProtocols {
                protocols {
                    id
                    name
                    actions {
                        id
                        name
                    }
                }
            }
        """

        result = await schema.execute(query, context_value=authenticated_context)

        assert result.data is not None
        assert "protocols" in result.data
        assert isinstance(result.data["protocols"], list)

    async def test_clients_query(self, authenticated_context: HttpContext):
        """Test fetching all clients via GraphQL query."""
        query = """
            query GetClients {
                clients {
                    id
                    clientId
                }
            }
        """

        result = await schema.execute(query, context_value=authenticated_context)

        assert result.data is not None
        assert "clients" in result.data
        assert isinstance(result.data["clients"], list)
