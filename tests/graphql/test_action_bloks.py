"""An action's own bloks, as GraphQL reads and guards them, and how a run finds its agents and space.

takt writes the rows at registration (``takt/crates/facade/tests/action_bloks.rs`` covers the
writes, the sharing and the collection); here they are seeded as takt leaves them. An action's
blok is read through ``Action.bloks`` and ``Task.actionBloks``, never listed with ``bloks``, and
nothing materializes, edits, forks, updates or deletes it. ``Task.dependencyAgents`` reads the
tree takt froze on a real assign; ``spacesForAgents`` ranks the spaces that place them.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Release
from django.utils import timezone
from kante.context import HttpContext

from facade import models
from facade.schema import schema
from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel
from tests.graphql_ops import BLOKS, CREATE_BLOK, DELETE_BLOK, FORK_BLOK, MATERIALIZE_BLOK, UPDATE_BLOK
from tests.registered import create_implementation

OVERLAY = [{"id": "root", "component": "Overlay", "children": [{"id": "at", "component": "Anchor", "props": [{"key": "dep", "static_value": "relay"}]}]}]

ACTION = """
query Action($id: ID!) {
  action(id: $id) { bloks { context blok { id heldByAction provided context componentTree vocab locals { key kind default persist } } } }
}
"""

TASK = """
query Task($id: ID!) {
  task(id: $id) { space { id name } actionBloks { context blok { id } } dependencyAgents { key agent { id } } }
}
"""

ASSIGN = "mutation Assign($input: AssignInput!) { assign(input: $input) { id } }"

EDIT = "mutation EditBlok($input: EditBlokInput!) { editBlok(input: $input) { applied } }"

SPACES = "query Spaces($agents: [ID!]!) { spacesForAgents(agents: $agents) { placed space { id name } } }"


def _definition(key: str) -> dict:
    return {"key": key, "version": "1", "name": key, "kind": "FUNCTION", "args": [], "returns": []}


def _agent(context: HttpContext, name: str) -> models.Agent:
    """A webhook agent of the request's user and organization: takt binds only an agent that can take work."""
    request = context.request
    client = Client.objects.create(client_id=f"{name}-client", device=Device.objects.create(device_id=f"{name}-device"))
    release = Release.objects.create(app=App.objects.create(identifier=f"{name}-app"), version="1.0.0")
    return models.Agent.objects.create(
        app=release.app, release=release, user=request.user, client=client, organization=request.organization, hash=f"{name}-hash", name=name, kind="WEBHOOK", hook_url="https://hook.example/in", connected=True, last_seen=timezone.now()
    )


def _seed(context: HttpContext) -> dict:
    """A workflow depending on a relay, and the workflow's overlay as takt writes it."""
    org, user = context.request.organization, context.request.user
    flow_agent, relay_agent = _agent(context, "flow"), _agent(context, "relay")
    workflow = create_implementation(
        ImplementationInputModel.model_validate({"interface": "workflow", "definition": _definition("workflow"), "dependencies": [{"key": "relay", "action_dependencies": [{"key": "relay"}]}]}),
        flow_agent,
    )
    relay = create_implementation(ImplementationInputModel.model_validate({"interface": "relay", "definition": _definition("relay")}), relay_agent)
    catalog = models.Module.objects.get_or_create(name="orkestrator", organization=org)[0]
    blok = models.Blok.objects.create(
        name="workflow:overlay",
        creator=user,
        organization=org,
        module=catalog,
        app=flow_agent.app,
        declaration_hash="b" * 64,
        context="OVERLAY",
        components=OVERLAY,
        locals=[{"key": "show_path", "kind": "BOOL", "default": True, "persist": True}],
        held_by_action=True,
    )
    models.ActionBlok.objects.create(action=workflow.action, context="OVERLAY", blok=blok)
    return {"workflow": workflow, "relay": relay, "blok": blok, "flow_agent": flow_agent, "relay_agent": relay_agent}


async def _run(context: HttpContext, operation: str, variables: dict) -> dict:
    result = await schema.execute(operation, context_value=context, variable_values=variables)
    assert result.errors is None, result.errors
    assert result.data is not None
    return result.data


def test_bloks_are_not_part_of_the_actions_hash() -> None:
    """An action with bloks is the action without them: the same hash, pinned as it was before bloks existed."""
    plain = DefinitionInputModel.model_validate(_definition("workflow"))
    with_bloks = DefinitionInputModel.model_validate({**_definition("workflow"), "bloks": [{"context": "OVERLAY", "vocab": 2, "components": OVERLAY}, {"context": "HTML", "vocab": 2, "source": "<Html><Card /></Html>"}]})
    assert with_bloks.unique_hash == plain.unique_hash == "fe79742535ec9df6b24d85b1d6b85f0e252173a39333bda6b3549e6bfe987932"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestActionBloks:
    async def test_an_action_shows_its_bloks_and_lists_never_do(self, authenticated_context: HttpContext) -> None:
        env = await sync_to_async(_seed)(authenticated_context)
        (shown,) = (await _run(authenticated_context, ACTION, {"id": str(env["workflow"].action_id)}))["action"]["bloks"]
        assert shown["context"] == "OVERLAY"
        assert shown["blok"] == {"id": str(env["blok"].id), "heldByAction": True, "provided": True, "context": "OVERLAY", "componentTree": OVERLAY, "vocab": 2, "locals": [{"key": "show_path", "kind": "BOOL", "default": True, "persist": True}]}

        listed = (await _run(authenticated_context, BLOKS, {}))["bloks"]
        assert "workflow:overlay" not in [blok["name"] for blok in listed]

    async def test_a_catalog_lists_no_actions_blok_and_takes_it_along(self, authenticated_context: HttpContext) -> None:
        env = await sync_to_async(_seed)(authenticated_context)
        catalogs = (await _run(authenticated_context, "query { modules { name bloks { id } } }", {}))["modules"]
        assert [catalog["bloks"] for catalog in catalogs if catalog["name"] == "orkestrator"] == [[]]
        # Its catalog going takes the blok, and the action's hold on it, along.
        await models.Module.objects.filter(name="orkestrator", organization=authenticated_context.request.organization).adelete()
        assert not await models.Blok.objects.filter(id=env["blok"].id).aexists()
        assert not await models.ActionBlok.objects.filter(action_id=env["workflow"].action_id).aexists()
        assert await models.Action.objects.filter(id=env["workflow"].action_id).aexists()

    async def test_nothing_changes_an_actions_blok_by_hand(self, authenticated_context: HttpContext) -> None:
        env = await sync_to_async(_seed)(authenticated_context)
        blok, agent = str(env["blok"].id), str(env["relay_agent"].id)
        refused = {
            "materializeBlok": (MATERIALIZE_BLOK, {"blok": blok, "agentMappings": [{"key": "relay", "agent": agent}]}),
            "materializeBlok for a message": (MATERIALIZE_BLOK, {"blok": blok, "scope": "MESSAGE", "agentMappings": []}),
            "editBlok": (EDIT, {"blok": blok, "locals": [{"key": "show_path", "kind": "BOOL", "default": False}]}),
            "forkBlok": (FORK_BLOK, {"id": blok, "name": "mine"}),
            "updateBlok": (UPDATE_BLOK, {"id": blok, "description": "mine now"}),
            "deleteBlok": (DELETE_BLOK, {"id": blok}),
        }
        for name, (operation, variables) in refused.items():
            result = await schema.execute(operation, context_value=authenticated_context, variable_values={"input": variables})
            assert result.errors, name
        assert await models.MaterializedBlok.objects.filter(blok_id=env["blok"].id).acount() == 0
        assert await models.Blok.objects.filter(held_by_action=False, app__isnull=True).acount() == 0, "nothing was forked"
        assert await models.Blok.objects.filter(id=env["blok"].id).aexists()

    async def test_an_overlay_and_a_run_reading_component_are_an_actions_only(self, authenticated_context: HttpContext, takt: str) -> None:
        overlay = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "mine", "vocab": 2, "context": "OVERLAY", "components": [{"id": "root", "component": "Overlay"}]}})
        assert overlay.errors and "an action's own" in overlay.errors[0].message
        asking = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "mine", "vocab": 2, "source": "<Html><Card><ask /></Card></Html>"}})
        assert asking.errors and "shows a task's open ask" in asking.errors[0].message
        anchored = await schema.execute(CREATE_BLOK, context_value=authenticated_context, variable_values={"input": {"name": "mine", "vocab": 2, "context": "SCENE", "source": '<Scene><Anchor dep="relay" /></Scene>'}})
        assert anchored.errors and "reads the agents of a run" in anchored.errors[0].message

    async def test_a_run_knows_its_agents_its_bloks_and_its_space(self, authenticated_context: HttpContext, takt: str) -> None:
        await schema.execute("query { __typename }", context_value=authenticated_context)
        env = await sync_to_async(_seed)(authenticated_context)
        relay_agent = str(env["relay_agent"].id)
        pin = [{"key": "relay", "mappedAgents": [{"key": "relay", "agent": relay_agent}]}]
        # A real assign: takt resolves and freezes the tree the run got.
        task = (await _run(authenticated_context, ASSIGN, {"input": {"implementation": str(env["workflow"].pk), "args": {}, "dependencies": pin}}))["assign"]["id"]

        run = (await _run(authenticated_context, TASK, {"id": task}))["task"]
        assert run["dependencyAgents"] == [{"key": "relay", "agent": {"id": relay_agent}}]
        assert run["actionBloks"] == [{"context": "OVERLAY", "blok": {"id": str(env["blok"].id)}}]
        assert run["space"] is None

        # The space a run was started in is the UI's: read back from the row.
        lab = await models.Space.objects.acreate(name="lab", creator=authenticated_context.request.user, organization=authenticated_context.request.organization)
        await models.Task.objects.filter(pk=task).aupdate(space=lab)
        assert (await _run(authenticated_context, TASK, {"id": task}))["task"]["space"] == {"id": str(lab.pk), "name": "lab"}
        # It goes, and the run stays without one.
        await lab.adelete()
        assert (await _run(authenticated_context, TASK, {"id": task}))["task"]["space"] is None

    async def test_spaces_are_ranked_by_how_many_of_the_agents_they_place(self, authenticated_context: HttpContext) -> None:
        env = await sync_to_async(_seed)(authenticated_context)
        request = authenticated_context.request
        flow, relay = env["flow_agent"], env["relay_agent"]

        def spaces() -> dict[str, models.Space]:
            made = {name: models.Space.objects.create(name=name, creator=request.user, organization=request.organization) for name in ("both", "one", "none")}
            models.Placement.objects.create(space=made["both"], agent=flow, role="flow")
            models.Placement.objects.create(space=made["both"], agent=relay, role="relay")
            # Placed twice: still one agent.
            models.Placement.objects.create(space=made["both"], agent=relay, role="spare")
            models.Placement.objects.create(space=made["one"], agent=relay, role="relay")
            return made

        made = await sync_to_async(spaces)()
        ranked = (await _run(authenticated_context, SPACES, {"agents": [str(flow.pk), str(relay.pk)]}))["spacesForAgents"]
        assert [(entry["space"]["name"], entry["placed"]) for entry in ranked] == [("both", 2), ("one", 1)]
        assert ranked[0]["space"]["id"] == str(made["both"].pk)
        assert (await _run(authenticated_context, SPACES, {"agents": []}))["spacesForAgents"] == []
