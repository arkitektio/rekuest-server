"""Where agents stand in a space (a room): kept per agent, the same for everyone, found by reference.

An action's ``<Space room=...>`` blok decides what a 3D view shows; the server only keeps where
people left each device. The first pose kept makes the space its ``room`` names.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Organization, Release, User
from kante.context import HttpContext

from facade.models import Agent, Caller, Placement, Space
from facade.schema import schema
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, TEST_TOKEN, tenant_context

SAVE = "mutation($input: SavePlacementInput!) { savePlacement(input: $input) { id role affineMatrix agent { id } space { id reference } } }"
RESET = "mutation($input: ResetPlacementInput!) { resetPlacement(input: $input) }"
ROOM = "query($room: String!) { spaceByReference(room: $room) { id reference placements { role affineMatrix agent { id } } } }"

SHIFTED = [[1.0, 0.0, 0.0, 0.8], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.15], [0.0, 0.0, 0.0, 1.0]]
MOVED = [[1.0, 0.0, 0.0, 2.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _agent_in(org: Organization, user: User, prefix: str) -> Agent:
    """An agent of ``user`` in ``org`` with its own client, app and release."""
    client = Client.objects.create(client_id=f"{prefix}-client", device=Device.objects.create(device_id=f"{prefix}-device"))
    Caller.objects.create(client=client, user=user, organization=org)
    release = Release.objects.create(app=App.objects.create(identifier=f"{prefix}-app"), version="1.0.0")
    return Agent.objects.create(app=release.app, hash=f"{prefix}-hash", release=release, user=user, client=client, organization=org)


@sync_to_async
def _seed(token: str = TEST_TOKEN) -> tuple[HttpContext, Agent]:
    """An agent in the token's organization."""
    context, user, _client, org = tenant_context(token)
    return context, _agent_in(org, user, f"arm-{token}")


async def _run(context: HttpContext, query: str, variables: dict) -> dict:
    result = await schema.execute(query, context_value=context, variable_values=variables)
    assert not result.errors, result.errors
    return result.data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestSpacePlacements:
    async def test_a_room_is_made_by_the_first_pose_kept_in_it(self, backend_stack) -> None:
        context, arm = await _seed()
        assert (await _run(context, ROOM, {"room": "room:helsinki-lab"}))["spaceByReference"] is None

        kept = (await _run(context, SAVE, {"input": {"room": "room:helsinki-lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": SHIFTED}}))["savePlacement"]
        assert (kept["role"], kept["affineMatrix"], kept["agent"]["id"], kept["space"]["reference"]) == ("robot", SHIFTED, str(arm.pk), "room:helsinki-lab")

        room = (await _run(context, ROOM, {"room": "room:helsinki-lab"}))["spaceByReference"]
        assert room["placements"] == [{"role": "robot", "affineMatrix": SHIFTED, "agent": {"id": str(arm.pk)}}]

    async def test_moving_a_device_again_keeps_one_pose_per_agent(self, backend_stack) -> None:
        context, arm = await _seed()
        first = (await _run(context, SAVE, {"input": {"room": "lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": SHIFTED}}))["savePlacement"]
        again = (await _run(context, SAVE, {"input": {"room": "lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": MOVED}}))["savePlacement"]

        assert again["id"] == first["id"] and again["affineMatrix"] == MOVED
        assert await Placement.objects.acount() == 1
        assert await Space.objects.acount() == 1

    async def test_reset_forgets_the_pose_so_the_blok_decides_again(self, backend_stack) -> None:
        context, arm = await _seed()
        kept = (await _run(context, SAVE, {"input": {"room": "lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": SHIFTED}}))["savePlacement"]

        assert (await _run(context, RESET, {"input": {"room": "lab", "agent": str(arm.pk)}}))["resetPlacement"] == kept["id"]
        assert (await _run(context, RESET, {"input": {"room": "lab", "agent": str(arm.pk)}}))["resetPlacement"] is None
        assert (await _run(context, ROOM, {"room": "lab"}))["spaceByReference"]["placements"] == []

    async def test_a_matrix_that_is_not_4x4_is_refused(self, backend_stack) -> None:
        context, arm = await _seed()
        refused = await schema.execute(SAVE, context_value=context, variable_values={"input": {"room": "lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": [[1.0, 0.0], [0.0, 1.0]]}})
        assert refused.errors and "4 rows of 4" in refused.errors[0].message

    async def test_another_organization_has_its_own_rooms_and_cannot_place_ours(self, backend_stack) -> None:
        context, arm = await _seed()
        other_context, _theirs = await _seed(OTHER_TOKEN)
        await _run(context, SAVE, {"input": {"room": "room:helsinki-lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": SHIFTED}})

        assert (await _run(other_context, ROOM, {"room": "room:helsinki-lab"}))["spaceByReference"] is None
        refused = await schema.execute(SAVE, context_value=other_context, variable_values={"input": {"room": "room:helsinki-lab", "agent": str(arm.pk), "role": "robot", "affineMatrix": MOVED}})
        assert refused.errors and "No agent" in refused.errors[0].message
        assert (await _run(other_context, RESET, {"input": {"room": "room:helsinki-lab", "agent": str(arm.pk)}}))["resetPlacement"] is None
        assert await Placement.objects.filter(affine_matrix=SHIFTED).aexists()
