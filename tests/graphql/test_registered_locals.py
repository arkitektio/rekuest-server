"""An agent's blok registered through implementAgent keeps its locals as sent and stores them
resolved beside: takt infers what Python sent left out.

Python sends a local whose kind is left out with only the fields its author gave (the model's
serializer); takt stores it so (``locals``) and, resolved at registration, every local kinded
(``resolved_locals``). A field the author left out (``nullable``) is filled by what the tree
tells, never by a default.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok
from facade.schema import schema
from tests.graphql_ops import ENSURE_AGENT

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
    "components": [{"id": "html", "component": "Html", "children": [{"id": "go", "component": "Button", "props": [{"key": "onClick", "agentCall": {"dependency": "self", "operation": "move_to", "arguments": [{"key": "image", "valuePath": "picked"}]}}]}]}],
    "locals": [{"key": "picked"}],
}

LEVEL = {
    "key": "level",
    "vocab": 2,
    "components": [{"id": "html", "component": "Html", "children": [{"id": "s", "component": "Slider", "props": [{"key": "bind", "dynamicValue": {"path": "level"}}]}]}],
    "locals": [{"key": "level", "persist": True}],
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRegisteredLocals:
    async def test_a_registered_blok_stores_its_locals_as_sent_and_resolved(self, authenticated_context: HttpContext) -> None:
        ensured = await schema.execute(ENSURE_AGENT, context_value=authenticated_context, variable_values={"input": {"name": "Resolver"}})
        assert not ensured.errors, ensured.errors
        implemented = await schema.execute(IMPLEMENT_AGENT, context_value=authenticated_context, variable_values={"input": {"bloks": [PICKER, LEVEL]}})
        assert not implemented.errors, implemented.errors

        written = await sync_to_async(lambda: {blok.name: blok.locals for blok in Blok.objects.all()})()
        assert written["picker"] == [{"key": "picked"}]
        assert written["level"] == [{"key": "level", "persist": True}]
        resolved = await sync_to_async(lambda: {blok.name: blok.resolved_locals for blok in Blok.objects.all()})()
        assert resolved["picker"] == [{"key": "picked", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": True, "persist": False}]
        assert resolved["level"] == [{"key": "level", "kind": "FLOAT", "nullable": False, "persist": True}]
