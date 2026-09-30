"""GraphQL agent filtering by state demands.

Pins the fix of ``AgentFilter.state_demands``: the old code targeted a nonexistent
``facade_stateschema`` table (always a runtime error) and OR-unioned the demands; it now
matches ``facade_statedefinition`` with one ``Exists()`` per demand — the agent must satisfy
EVERY demand, each possibly via a different State.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from authentikate.models import App, Client, Release

from facade import models
from facade.schema import schema

AGENTS_QUERY = """
    query AgentsByStateDemands($demands: [StateDemandInput!]) {
        agents(filters: { stateDemands: $demands }) {
            id
            name
        }
    }
"""


def _seed_agents(context):
    request = context.request
    org = request.organization

    counter_definition = models.StateDefinition.objects.create(
        organization=org,
        name="Counter",
        hash="agents-sd-counter",
        description="counter",
        ports=[{"key": "count", "kind": "INT", "identifier": None, "nullable": False, "children": []}],
    )

    agents = {}
    for name, with_state in [("stateful", True), ("stateless", False)]:
        app = App.objects.create(identifier=f"asd-{name}-app")
        release = Release.objects.create(app=app, version="1.0.0")
        # Agents are unique per (client, user, organization) — one client per agent.
        client = Client.objects.create(client_id=f"asd-{name}-client")
        agent = models.Agent.objects.create(name=name, app=app, release=release, user=request.user, client=client, organization=org, hash=f"asd-{name}-hash")
        if with_state:
            models.State.objects.create(definition=counter_definition, interface="counter", agent=agent)
        agents[name] = agent
    return agents


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_agents_filtered_by_state_demands(authenticated_context: HttpContext):
    await schema.execute("query { __typename }", context_value=authenticated_context)
    await sync_to_async(_seed_agents)(authenticated_context)

    result = await schema.execute(
        AGENTS_QUERY,
        context_value=authenticated_context,
        variable_values={"demands": [{"matches": [{"kind": "INT"}]}]},
    )

    assert result.errors is None, result.errors
    names = [agent["name"] for agent in result.data["agents"]]
    assert "stateful" in names
    assert "stateless" not in names

    # A demand no StateDefinition satisfies filters out every agent.
    result = await schema.execute(
        AGENTS_QUERY,
        context_value=authenticated_context,
        variable_values={"demands": [{"matches": [{"kind": "DATE"}]}]},
    )
    assert result.errors is None, result.errors
    assert result.data["agents"] == []
