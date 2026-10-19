"""Provided bloks through the API: an app's agents own them, users fork them.

A blok an agent declares is its app's (``app`` set) and changes only with its agents'
registrations (takt's side is ``takt/crates/facade/tests/provided_bloks.rs``). Here: users cannot
edit, delete or materialize one, nor touch an agent's own materialization of it; ``forkBlok``
copies it into a custom blok they can; and ``provided`` tells the two apart.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Release
from kante.context import HttpContext

from facade.models import Agent, Blok, BlokDependency, Caller, MaterializedBlok, UICatalog
from facade.schema import schema
from tests.graphql_ops import BLOKS, CREATE_BLOK, DELETE_BLOK, DELETE_MATERIALIZED_BLOK, FORK_BLOK, MATERIALIZE_BLOK, UPDATE_BLOK, UPDATE_MATERIALIZED_BLOK


@sync_to_async
def _provided(context: HttpContext) -> dict:
    """A blok the app ``viewer`` provides, held by one of its agents."""
    org, user = context.request.organization, context.request.user
    client = Client.objects.create(client_id="viewer-client", device=Device.objects.create(device_id="viewer-device"))
    Caller.objects.create(client=client, user=user, organization=org)
    release = Release.objects.create(app=App.objects.create(identifier="viewer"), version="1.0.0")
    agent = Agent.objects.create(app=release.app, hash="h", release=release, user=user, client=client, organization=org)
    catalog = UICatalog.objects.get_or_create(name="orkestrator", organization=org)[0]
    blok = Blok.objects.create(
        name="panel",
        creator=user,
        organization=org,
        catalog=catalog,
        app=release.app,
        declaration_hash="a" * 64,
        components=[{"id": "root", "component": "Box"}],
    )
    BlokDependency.objects.create(blok=blok, key="self")
    slot = MaterializedBlok.objects.create(blok=blok, declared_by=agent, name="panel", description="")
    return {"blok": blok, "agent": agent, "slot": slot}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestProvidedBloks:
    async def test_a_provided_blok_is_not_changed_by_hand(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        blok, slot, agent = str(env["blok"].id), str(env["slot"].id), str(env["agent"].id)
        refused = {
            "updateBlok": (UPDATE_BLOK, {"id": blok, "description": "mine now"}),
            "deleteBlok": (DELETE_BLOK, {"id": blok}),
            "materializeBlok": (MATERIALIZE_BLOK, {"blok": blok, "agentMappings": [{"key": "self", "agent": agent}]}),
            "updateMaterializedBlok": (UPDATE_MATERIALIZED_BLOK, {"id": slot, "agentMappings": []}),
            "deleteMaterializedBlok": (DELETE_MATERIALIZED_BLOK, {"id": slot}),
        }
        for name, (operation, variables) in refused.items():
            result = await schema.execute(operation, context_value=authenticated_context, variable_values={"input": variables})
            assert result.errors, name
        assert await sync_to_async(MaterializedBlok.objects.filter(id=env["slot"].id).exists)()
        assert await sync_to_async(lambda: Blok.objects.get(id=env["blok"].id).description)() is None

    async def test_create_blok_never_replaces_a_provided_one(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        created = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "panel", "description": "mine"}})
        assert not created.errors, created.errors
        assert created.data["createBlok"]["id"] != str(env["blok"].id)
        assert await sync_to_async(Blok.objects.filter(name="panel").count)() == 2

    async def test_a_fork_is_a_custom_copy(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        forked = await schema.execute(FORK_BLOK, context_value=authenticated_context, variable_values={"input": {"id": str(env["blok"].id), "name": "my-panel"}})
        assert not forked.errors, forked.errors
        fork = forked.data["forkBlok"]
        assert fork["name"] == "my-panel"
        assert fork["provided"] is False
        assert fork["componentTree"] == [{"id": "root", "component": "Box"}]
        assert fork["dependencies"] == [{"key": "self"}]

        # The copy is the user's: editable, and it outlives the provided one.
        updated = await schema.execute(UPDATE_BLOK, context_value=authenticated_context, variable_values={"input": {"id": fork["id"], "description": "mine"}})
        assert not updated.errors, updated.errors
        await sync_to_async(env["blok"].delete)()
        assert await sync_to_async(Blok.objects.filter(id=fork["id"]).exists)()

        again = await schema.execute(FORK_BLOK, context_value=authenticated_context, variable_values={"input": {"id": fork["id"]}})
        assert again.errors, "a custom blok is not forked"

    async def test_provided_tells_the_two_apart(self, authenticated_context: HttpContext) -> None:
        env = await _provided(authenticated_context)
        created = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "custom"}})
        assert not created.errors, created.errors

        for provided, expected in [
            (True, [{"name": "panel", "provided": True, "app": {"identifier": "viewer"}, "providers": [{"id": str(env["agent"].id)}]}]),
            (False, [{"name": "custom", "provided": False, "app": None, "providers": []}]),
        ]:
            listed = await schema.execute(BLOKS, context_value=authenticated_context, variable_values={"filters": {"provided": provided}})
            assert not listed.errors, listed.errors
            assert listed.data["bloks"] == expected
