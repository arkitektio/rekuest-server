"""GraphQL UI-component operations: bloks and dashboards."""

import pytest
from kante.context import HttpContext

from facade.schema import schema

from tests.graphql_ops import CREATE_BLOK, CREATE_DASHBOARD


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestGraphQLUI:
    """GraphQL operations over UI components (Blok / Dashboard)."""

    async def test_bloks_query(self, authenticated_context: HttpContext):
        """Test fetching all UI bloks via GraphQL query."""
        query = """
            query GetBloks {
                bloks {
                    id
                    name
                    creator {
                        sub
                    }
                }
            }
        """

        result = await schema.execute(query, context_value=authenticated_context)

        assert result.data is not None
        assert "bloks" in result.data
        assert isinstance(result.data["bloks"], list)

    async def test_create_blok_mutation(self, authenticated_context: HttpContext):
        """Test creating a UI blok via mutation."""
        result = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "Test UI Blok", "description": "A test UI component", "vocab": 2}})

        assert result.data is not None, f"Errors: {result.errors}"
        assert "createBlok" in result.data

        blok = result.data["createBlok"]
        assert blok["name"] == "Test UI Blok"
        assert blok["description"] == "A test UI component"

    async def test_create_dashboard_mutation(self, authenticated_context: HttpContext):
        """Test creating a dashboard via mutation."""
        result = await schema.execute(CREATE_DASHBOARD, context_value=authenticated_context, variable_values={"input": {"name": "Test Dashboard"}})

        assert result.data is not None, f"Errors: {result.errors}"
        assert "createDashboard" in result.data

        dashboard = result.data["createDashboard"]
        assert dashboard["name"] == "Test Dashboard"
