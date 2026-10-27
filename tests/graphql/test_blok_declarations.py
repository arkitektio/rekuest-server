"""What a v2 blok declares beside its tree, through the API: locals, demo, queries, subscriptions,
catalogs and the language version.

createBlok stores them on the blok and its first revision; an edit that leaves one out keeps it;
what is malformed is refused with the reason; an editBlok dry run reports every finding of the
tree, errors included, with the vocabulary's code, and keeps nothing.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import BlokRevision
from facade.schema import schema

DECLARED = """
    vocab
    catalogs
    locals { key kind default persist }
    demo { deps args task queries subscriptions }
    queries { key service document variables { key valuePath } poll more }
    subscriptions { key service document keep history }
"""

CREATE = f"""
mutation CreateBlok($input: CreateBlokInput!) {{
  createBlok(input: $input) {{ id {DECLARED} head {{ id {DECLARED} }} }}
}}
"""

EDIT = f"""
mutation EditBlok($input: EditBlokInput!) {{
  editBlok(input: $input) {{
    applied
    blok {{ id {DECLARED} }}
    revision {{ number {DECLARED} }}
    diagnostics {{ level code message path }}
  }}
}}
"""

TREE = [{"id": "html", "component": "Html", "children": [{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamicValue": {"path": "queries.positions.status"}}]}]}]
POSITIONS = {"key": "positions", "service": "mikro", "document": "query($slide: ID!) { positions(slide: $slide) { id } }", "variables": [{"key": "slide", "valuePath": "args.slide"}], "poll": 10, "more": "positions"}
EVENTS = {"key": "events", "service": "rekuest", "document": "subscription { events { id } }", "keep": 20, "history": "positions"}
DECLARATIONS = {
    "vocab": 2,
    "catalogs": ["ui@1"],
    "locals": [{"key": "iterations", "kind": "INT", "default": 15, "persist": True}],
    "demo": {"args": {"slide": "1"}, "queries": {"positions": [{"id": "p"}]}},
    "queries": [POSITIONS],
    "subscriptions": [EVENTS],
}
EXPECTED = {
    "vocab": 2,
    "catalogs": ["ui@1"],
    "locals": [{"key": "iterations", "kind": "INT", "default": 15, "persist": True}],
    "demo": {"deps": None, "args": {"slide": "1"}, "task": None, "queries": {"positions": [{"id": "p"}]}, "subscriptions": None},
    "queries": [{"key": "positions", "service": "mikro", "document": POSITIONS["document"], "variables": [{"key": "slide", "valuePath": "args.slide"}], "poll": 10.0, "more": "positions"}],
    "subscriptions": [{"key": "events", "service": "rekuest", "document": EVENTS["document"], "keep": 20, "history": "positions"}],
}


async def _run(context: HttpContext, operation: str, variables: dict) -> dict:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


async def _refused(context: HttpContext, operation: str, variables: dict) -> str:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert result.errors, "expected the write to be refused"
    return result.errors[0].message


def _declared(blok: dict) -> dict:
    return {key: blok[key] for key in EXPECTED}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokDeclarations:
    async def test_declarations_are_stored_read_back_and_kept(self, authenticated_context: HttpContext) -> None:
        created = (await _run(authenticated_context, CREATE, {"input": {"name": "positions", "components": TREE, **DECLARATIONS}}))["createBlok"]
        assert _declared(created) == EXPECTED
        assert _declared(created["head"]) == EXPECTED

        # An edit of the tree alone keeps everything declared beside it.
        tree = [{"id": "html", "component": "Html", "children": [{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamicValue": {"path": "iterations"}}]}]}]
        edited = (await _run(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": created["head"]["id"], "components": tree}}))["editBlok"]
        assert edited["applied"] is True
        assert edited["revision"]["number"] == 2
        assert _declared(edited["revision"]) == EXPECTED
        assert _declared(edited["blok"]) == EXPECTED

        # One part given replaces that part only.
        locals_only = [{"key": "iterations", "kind": "INT", "default": 3}]
        relocal = (await _run(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": (await self._head(created["id"])), "locals": locals_only}}))["editBlok"]
        assert relocal["blok"]["locals"] == [{"key": "iterations", "kind": "INT", "default": 3, "persist": False}]
        assert relocal["blok"]["queries"] == EXPECTED["queries"]

    @staticmethod
    @sync_to_async
    def _head(blok_id: str) -> str:
        return str(BlokRevision.objects.filter(blok_id=blok_id).order_by("-number").first().id)

    async def test_malformed_declarations_are_refused(self, authenticated_context: HttpContext) -> None:
        for declarations, message in (
            ({"locals": [{"key": "task", "kind": "STRING"}]}, "'task' is reserved: no local, let or into may use it. [roots.reserved]"),
            ({"queries": [{**POSITIONS, "document": "mutation { delete }"}]}, "is a mutation: a blok reads, and writes only through a task [query.read_only]"),
            ({"subscriptions": [{**EVENTS, "history": "nope"}]}, "replays from 'nope', which is no declared query. [query.history]"),
            ({"catalogs": ["ui"]}, "is not name@version (ui@1). [manifest.catalog]"),
            ({"queries": [{**POSITIONS, "more": "positions[0]"}]}, "is not a field path into its data (positions, images.items). [query.more]"),
            ({"vocab": 1}, "2"),
        ):
            message_got = await _refused(authenticated_context, CREATE, {"input": {"name": "broken", "components": TREE, **{**DECLARATIONS, **declarations}}})
            assert message in message_got, (declarations, message_got)

    async def test_a_dry_run_reports_vocabulary_codes_and_keeps_nothing(self, authenticated_context: HttpContext) -> None:
        created = (await _run(authenticated_context, CREATE, {"input": {"name": "positions", "components": TREE, **DECLARATIONS}}))["createBlok"]
        tree = [
            {"id": "Html[0]", "component": "Html", "children": [
                {"id": "Html[0]/Card[0]", "component": "Card", "props": [{"key": "className", "staticValue": "bg-red-500"}], "children": [
                    {"id": "Html[0]/Card[0]/Text[0]", "component": "Text", "props": [{"key": "text", "dynamicValue": {"path": "nothing.here"}}]},
                ]},
            ]},
        ]
        checked = (await _run(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": created["head"]["id"], "components": tree, "dryRun": True}}))["editBlok"]
        assert checked["applied"] is False
        assert checked["revision"] is None
        codes = {(finding["level"], finding["code"]) for finding in checked["diagnostics"]}
        assert ("ERROR", "roots.unknown") in codes, checked["diagnostics"]
        assert ("WARNING", "palette.raw_colour") in codes, checked["diagnostics"]
        assert await sync_to_async(BlokRevision.objects.filter(blok_id=created["id"]).count)() == 1

        # The same edit for real is refused on the error.
        message = await _refused(authenticated_context, EDIT, {"input": {"blok": created["id"], "base": created["head"]["id"], "components": tree}})
        assert "reads 'nothing'" in message
