"""An agent's blok registered through implementAgent stores the ``<local>`` declarations of its
tree resolved: takt infers what the tree left out.

A ``<local>`` whose kind is left out says only the fields its author gave; takt stores every local
of the tree kinded, by its node id (``resolved_locals``). A field the author left out
(``nullable``) is filled by what the tree tells, never by a default.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok
from facade.schema import schema
from tests.graphql_ops import ENSURE_AGENT
from tests.registered import blok, gql, local, node

pytestmark = pytest.mark.usefixtures("takt")

IMPLEMENT_AGENT = """
    mutation ImplementAgent($input: ImplementAgentInput!) {
        implementAgent(input: $input) { id }
    }
"""

PICKER = {
    "key": "picker",
    "vocab": 2,
    "dependencies": [
        {
            "key": "self",
            "actionDependencies": [{"key": "move_to", "demand": {"argMatches": [{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True}]}}],
        }
    ],
    "components": [
        gql(
            blok(
                "agent",
                "panel",
                local("picked"),
                {"id": "go", "component": "Button", "props": [{"key": "onClick", "agent_call": {"dependency": "self", "operation": "move_to", "arguments": [{"key": "image", "valuePath": "picked"}]}}]},
            )
        )
    ],
}

LEVEL = {
    "key": "level",
    "vocab": 2,
    "components": [gql(blok("agent", "panel", local("level", persist=True), node("Slider", bind={"path": "level"})))],
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRegisteredLocals:
    async def test_a_registered_blok_stores_its_locals_resolved_by_node(self, authenticated_context: HttpContext) -> None:
        ensured = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Resolver"}})
        assert not ensured.errors, ensured.errors
        implemented = await schema.execute(IMPLEMENT_AGENT, context_value=authenticated_context, variable_values={"input": {"bloks": [PICKER, LEVEL]}})
        assert not implemented.errors, implemented.errors

        resolved = await sync_to_async(lambda: {row.name: row.resolved_locals for row in Blok.objects.all()})()
        assert resolved["picker"] == [{"node": "Blok[0]/local[0]", "key": "picked", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True, "persist": False}]
        # What a Slider writes is its catalog's to say: the server reads none, so the local stays ANY.
        assert resolved["level"] == [{"node": "Blok[0]/local[0]", "key": "level", "kind": "ANY", "nullable": False, "persist": True}]
