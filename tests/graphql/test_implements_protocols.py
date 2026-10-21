"""``implementsProtocols``: the agents offering an action listed under a protocol.

What a scene asks to find the agents it can bind: those with a hook hinted
``on_scene_init``. Each agent once, however many of its actions match; the
implementations of one agent narrow by ``action.protocols`` as before.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Release
from django.utils import timezone
from kante.context import HttpContext

from facade import models
from facade.schema import schema
from rekuest_core.inputs.models import ImplementationInputModel
from tests.registered import create_implementation

AGENTS = "query Agents($filters: AgentFilter) { agents(filters: $filters) { id } }"
IMPLEMENTATIONS = """
    query Implementations($filters: ImplementationFilter) {
        implementations(filters: $filters) { interface agent { id } action { protocols { name } } }
    }
"""


def _agent(context: HttpContext, name: str, hooks: dict[str, list[str]]) -> models.Agent:
    """An agent offering one action per ``hooks`` entry, listed under its protocols.

    ``create_implementation`` writes no protocol rows (registration in takt does), so
    they are added here the way takt adds them.
    """
    request = context.request
    device = Device.objects.create(device_id=f"{name}-device")
    client = Client.objects.create(client_id=f"{name}-client", device=device)
    app = App.objects.create(identifier=f"{name}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = models.Agent.objects.create(
        app=app,
        release=release,
        user=request.user,
        client=client,
        organization=request.organization,
        hash=f"{name}-hash",
        name=name,
        connected=True,
        last_seen=timezone.now(),
    )
    for interface, protocols in hooks.items():
        implementation = create_implementation(
            ImplementationInputModel.model_validate(
                {"interface": interface, "definition": {"key": f"{name}-{interface}", "version": "1", "name": interface, "kind": "FUNCTION", "args": [], "returns": []}}
            ),
            agent,
        )
        for protocol in protocols:
            row, _ = models.Protocol.objects.get_or_create(organization=request.organization, name=protocol, defaults={"description": "test"})
            implementation.action.protocols.add(row)
    return agent


async def _data(context: HttpContext, query: str, filters: dict) -> dict:
    result = await schema.execute(query, context_value=context, variable_values={"filters": filters})
    assert result.errors is None, result.errors
    return result.data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestImplementsProtocols:
    async def test_only_the_agents_offering_a_hinted_action_and_each_once(self, authenticated_context: HttpContext):
        def agents() -> tuple[models.Agent, models.Agent]:
            scene = _agent(authenticated_context, "proto-scene", {"attach": ["on_scene_init"], "segment": ["on_scene_annotation", "on_scene_init"]})
            _agent(authenticated_context, "proto-other", {"work": ["predicate"]})
            plain = _agent(authenticated_context, "proto-plain", {"work": []})
            return scene, plain

        scene, _ = await sync_to_async(agents)()

        data = await _data(authenticated_context, AGENTS, {"implementsProtocols": ["on_scene_init", "on_scene_leave"]})

        assert [a["id"] for a in data["agents"]] == [str(scene.id)]

    async def test_an_agent_s_hooks_narrow_by_protocol(self, authenticated_context: HttpContext):
        scene = await sync_to_async(_agent)(authenticated_context, "proto-hooks", {"attach": ["on_scene_init"], "segment": ["on_scene_annotation"]})

        data = await _data(
            authenticated_context,
            IMPLEMENTATIONS,
            {"agent": {"ids": [str(scene.id)], "implementsProtocols": ["on_scene_annotation"]}, "action": {"protocols": ["on_scene_annotation"]}},
        )

        assert [(i["interface"], i["action"]["protocols"]) for i in data["implementations"]] == [("segment", [{"name": "on_scene_annotation"}])]
