"""A materialization made for a chat message (``scope: MESSAGE``).

An agent that wants to show a blok in its message materializes it for that message, bound to its
own agents, and attaches it to the message. Such a materialization may be of a blok an app
provides, takes no dashboard, and never shows up in a list: it is opened by its id.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Dashboard, MaterializedBlok
from facade.schema import schema
from tests.graphql.test_provided_bloks import _provided
from tests.graphql_ops import MATERIALIZE_BLOK

LISTS = """
    query Lists($blok: ID!) {
        materializedBloks { id }
        blok(id: $blok) { materializedBloks { id } }
    }
"""

ONE = """
    query One($id: ID!) {
        materializedBlok(id: $id) { id scope agentMappings { key agent { id } } }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestMessageMaterializations:
    async def test_a_provided_blok_is_materialized_for_a_message(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        blok, agent = str(env["blok"].id), str(env["agent"].id)

        made = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": blok, "scope": "MESSAGE", "agentMappings": [{"key": "self", "agent": agent}]}},
        )
        assert not made.errors, made.errors
        made_id = made.data["materializeBlok"]["id"]

        opened = await schema.execute(ONE, context_value=authenticated_context, variable_values={"id": made_id})
        assert not opened.errors, opened.errors
        assert opened.data["materializedBlok"] == {"id": made_id, "scope": "MESSAGE", "agentMappings": [{"key": "self", "agent": {"id": agent}}]}

        listed = await schema.execute(LISTS, context_value=authenticated_context, variable_values={"blok": blok})
        assert not listed.errors, listed.errors
        assert made_id not in {row["id"] for row in listed.data["materializedBloks"]}
        assert made_id not in {row["id"] for row in listed.data["blok"]["materializedBloks"]}
        # The agent's own materialization is still listed.
        assert str(env["slot"].id) in {row["id"] for row in listed.data["materializedBloks"]}

    async def test_a_message_materialization_takes_no_dashboard(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        dashboard = await sync_to_async(Dashboard.objects.create)(name="board", organization=authenticated_context.request.organization)

        refused = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": str(env["blok"].id), "scope": "MESSAGE", "dashboard": str(dashboard.id), "agentMappings": [{"key": "self", "agent": str(env["agent"].id)}]}},
        )
        assert refused.errors
        assert await sync_to_async(MaterializedBlok.objects.filter(scope="MESSAGE").count)() == 0
