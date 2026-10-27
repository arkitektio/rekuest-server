"""A custom blok installed from an origin: kabinet's repository bloks.

``createBlok`` with an ``origin`` finds the blok again by it, so installing again updates the same
row (its materializations stay), whatever it is named now. It never replaces a blok made by hand
under the same name. ``originHash`` is what the origin gave what it draws, so a client tells an
update apart from an installed copy.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok
from facade.schema import schema

CREATE = """
mutation CreateBlok($input: CreateBlokInput!) {
  createBlok(input: $input) { id name origin originHash }
}
"""

BLOKS = """
query Bloks($filters: BlokFilter) {
  bloks(filters: $filters) { id name origin }
}
"""

ORIGIN = "github:arkitektio/bloks:main#layers"
TREE = [{"id": "Html[0]", "component": "Html", "children": [{"id": "Html[0]/Slider[0]", "component": "Slider"}]}]


async def _create(context: HttpContext, **fields) -> dict:
    result = await schema.execute(CREATE, context_value=context, variable_values={"input": {"vocab": 2, "components": TREE, **fields}})
    assert not result.errors, result.errors
    return result.data["createBlok"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokOrigin:
    async def test_installing_again_updates_in_place(self, authenticated_context: HttpContext) -> None:
        first = await _create(authenticated_context, name="Layer picker", origin=ORIGIN, originHash="sha256:a")
        second = await _create(authenticated_context, name="Layers", origin=ORIGIN, originHash="sha256:b")

        assert second["id"] == first["id"]
        assert (second["name"], second["origin"], second["originHash"]) == ("Layers", ORIGIN, "sha256:b")
        assert await sync_to_async(Blok.objects.filter(origin=ORIGIN).count)() == 1

    async def test_a_blok_made_by_hand_is_never_replaced(self, authenticated_context: HttpContext) -> None:
        mine = await _create(authenticated_context, name="Layer picker")

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Layer picker", "vocab": 2, "components": TREE, "origin": ORIGIN, "originHash": "sha256:a"}},
        )

        assert result.errors and "exists already" in result.errors[0].message
        assert (await sync_to_async(Blok.objects.get)(id=mine["id"])).origin is None

    async def test_without_an_origin_it_is_still_by_name(self, authenticated_context: HttpContext) -> None:
        first = await _create(authenticated_context, name="Plain")
        second = await _create(authenticated_context, name="Plain")

        assert second["id"] == first["id"]
        assert second["origin"] is None

    async def test_filters_by_origin(self, authenticated_context: HttpContext) -> None:
        installed = await _create(authenticated_context, name="Installed", origin=ORIGIN)
        await _create(authenticated_context, name="Mine")

        async def names(filters: dict) -> list[str]:
            result = await schema.execute(BLOKS, context_value=authenticated_context, variable_values={"filters": filters})
            assert not result.errors, result.errors
            return sorted(blok["name"] for blok in result.data["bloks"])

        assert await names({"hasOrigin": True}) == ["Installed"]
        assert await names({"hasOrigin": False, "provided": False}) == ["Mine"]
        assert await names({"origins": [ORIGIN]}) == [installed["name"]]
