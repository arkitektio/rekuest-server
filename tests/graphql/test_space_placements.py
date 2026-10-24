"""A 3D blok placed in a space at an affine transform, with no agent of its own."""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok, MaterializedBlok, Space, SpacePlacement, UICatalog
from facade.schema import schema
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, TEST_TOKEN, tenant_context

PLACE = "mutation($input: CreateSpacePlacementInput!) { createSpacePlacement(input: $input) { id name affineMatrix blok { id } space { id } } }"
MOVE = "mutation($input: UpdateSpacePlacementInput!) { updateSpacePlacement(input: $input) { id name affineMatrix } }"
REMOVE = "mutation($input: DeleteSpacePlacementInput!) { deleteSpacePlacement(input: $input) }"
SPACE = "query($id: ID!) { space(id: $id) { blokPlacements { id name affineMatrix blok { id } } } }"

SHIFTED = [[1.0, 0.0, 0.0, 10.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


@sync_to_async
def _seed(token: str = TEST_TOKEN) -> tuple[HttpContext, Space, MaterializedBlok]:
    """A space and a materialized 3D blok in the token's organization."""
    context, user, _client, org = tenant_context(token)
    catalog = UICatalog.objects.get_or_create(name="default", organization=org)[0]
    blok = Blok.objects.create(name=f"stage-{token}", creator=user, organization=org, catalog=catalog, context="SCENE")
    materialized = MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
    space = Space.objects.create(name=f"lab-{token}", creator=user, organization=org)
    return context, space, materialized


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestSpacePlacements:
    async def test_a_blok_is_placed_moved_and_removed(self, backend_stack) -> None:
        context, space, blok = await _seed()

        placed = await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(space.pk), "blok": str(blok.pk), "name": "stage", "affineMatrix": SHIFTED}})
        assert not placed.errors, placed.errors
        placement = placed.data["createSpacePlacement"]
        assert (placement["name"], placement["affineMatrix"], placement["blok"]["id"]) == ("stage", SHIFTED, str(blok.pk))

        listed = (await schema.execute(SPACE, context_value=context, variable_values={"id": str(space.pk)})).data["space"]["blokPlacements"]
        assert [row["id"] for row in listed] == [placement["id"]]

        moved = await schema.execute(MOVE, context_value=context, variable_values={"input": {"id": placement["id"], "affineMatrix": [[2.0, 0, 0, 0], [0, 2.0, 0, 0], [0, 0, 2.0, 0], [0, 0, 0, 1.0]]}})
        assert not moved.errors, moved.errors
        assert moved.data["updateSpacePlacement"]["affineMatrix"][0][0] == 2.0

        removed = await schema.execute(REMOVE, context_value=context, variable_values={"input": {"id": placement["id"]}})
        assert not removed.errors, removed.errors
        assert not await SpacePlacement.objects.aexists()

    async def test_an_identity_placement_needs_no_matrix(self, backend_stack) -> None:
        context, space, blok = await _seed()
        placed = await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(space.pk), "blok": str(blok.pk)}})
        assert not placed.errors, placed.errors
        assert placed.data["createSpacePlacement"]["affineMatrix"] is None

    async def test_a_matrix_that_is_not_4x4_is_refused(self, backend_stack) -> None:
        context, space, blok = await _seed()
        refused = await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(space.pk), "blok": str(blok.pk), "affineMatrix": [[1.0, 0.0], [0.0, 1.0]]}})
        assert refused.errors and "4 rows of 4" in refused.errors[0].message

    async def test_another_organization_can_neither_place_into_nor_use_a_space_or_blok(self, backend_stack) -> None:
        context, space, blok = await _seed()
        other_context, other_space, other_blok = await _seed(OTHER_TOKEN)

        into_theirs = await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(other_space.pk), "blok": str(blok.pk)}})
        their_blok = await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(space.pk), "blok": str(other_blok.pk)}})
        assert into_theirs.errors and "No space" in into_theirs.errors[0].message
        assert their_blok.errors and "No materialized blok" in their_blok.errors[0].message

        mine = (await schema.execute(PLACE, context_value=context, variable_values={"input": {"space": str(space.pk), "blok": str(blok.pk)}})).data["createSpacePlacement"]
        assert (await schema.execute(MOVE, context_value=other_context, variable_values={"input": {"id": mine["id"], "name": "taken"}})).errors
        assert (await schema.execute(REMOVE, context_value=other_context, variable_values={"input": {"id": mine["id"]}})).errors
        assert await SpacePlacement.objects.filter(id=mine["id"]).aexists()
