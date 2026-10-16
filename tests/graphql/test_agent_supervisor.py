"""An agent's self-declared supervisor: the ``supervisor`` field, and the filters pickers use.

``@rekuest/agent`` carries one descriptor, ``@rekuest/supervisor``. A picker hands a port's
constraints in as ``requires``; any other key is refused, never ignored.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Release
from kante.context import HttpContext

from facade.models import Agent
from facade.schema import schema

pytestmark = pytest.mark.usefixtures("takt")

AGENTS = """
    query Agents($filters: AgentFilter) {
        agents(filters: $filters) {
            id
            supervisor
        }
    }
"""


def _make_agent(context: HttpContext, name: str, supervisor: str | None) -> str:
    """An agent of the request's organization, on a client of its own (one per agent)."""
    request = context.request
    device = Device.objects.create(device_id=f"{name}-device")
    client = Client.objects.create(client_id=f"{name}-client", device=device)
    app = App.objects.create(identifier=f"{name}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    # takt writes the supervisor from REGISTER; here the row is made directly.
    agent = Agent.objects.create(
        app=app, release=release, user=request.user, client=client, organization=request.organization, hash=f"{name}-hash", name=name, supervisor=supervisor
    )
    return str(agent.id)


_agent = sync_to_async(_make_agent)


async def _ids(context: HttpContext, filters: dict) -> set[str]:
    result = await schema.execute(AGENTS, context_value=context, variable_values={"filters": filters})
    assert result.data is not None, result.errors
    return {agent["id"] for agent in result.data["agents"]}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentSupervisor:
    async def test_the_agent_shows_its_supervisor(self, authenticated_context: HttpContext):
        managed = await _agent(authenticated_context, "managed", "plugin_engine")

        result = await schema.execute(AGENTS, context_value=authenticated_context, variable_values={"filters": {"ids": [managed]}})

        assert result.data is not None, result.errors
        assert result.data["agents"] == [{"id": managed, "supervisor": "plugin_engine"}]

    async def test_requires_narrows_by_the_supervisor(self, authenticated_context: HttpContext):
        managed = await _agent(authenticated_context, "managed", "plugin_engine")
        elsewhere = await _agent(authenticated_context, "elsewhere", "local_launcher")
        free = await _agent(authenticated_context, "free", None)
        mine = {managed, elsewhere, free}

        def requires(operator: str, value: object = None) -> dict:
            return {"requires": [{"key": "@rekuest/supervisor", "operator": operator, "value": value}]}

        assert await _ids(authenticated_context, requires("EQUALS", "plugin_engine")) & mine == {managed}
        assert await _ids(authenticated_context, requires("MATCHES", "plugin_engine")) & mine == {managed}
        assert await _ids(authenticated_context, requires("IN", ["plugin_engine", "local_launcher"])) & mine == {managed, elsewhere}
        assert await _ids(authenticated_context, requires("EXISTS")) & mine == {managed, elsewhere}
        # An agent that declares none has no value: only the negations hold for it.
        assert await _ids(authenticated_context, requires("NOT_EQUALS", "plugin_engine")) & mine == {elsewhere, free}
        assert await _ids(authenticated_context, requires("NOT_IN", ["plugin_engine"])) & mine == {elsewhere, free}
        assert await _ids(authenticated_context, {"supervisor": "local_launcher"}) & mine == {elsewhere}

    async def test_a_key_the_agent_does_not_carry_is_refused(self, authenticated_context: HttpContext):
        await _agent(authenticated_context, "managed", "plugin_engine")

        result = await schema.execute(
            AGENTS,
            context_value=authenticated_context,
            variable_values={"filters": {"requires": [{"key": "@mikro/n_channels", "operator": "EQUALS", "value": 3}]}},
        )

        assert result.errors and "declares no descriptor" in str(result.errors[0])

    async def test_an_operator_a_string_cannot_take_is_refused(self, authenticated_context: HttpContext):
        result = await schema.execute(
            AGENTS,
            context_value=authenticated_context,
            variable_values={"filters": {"requires": [{"key": "@rekuest/supervisor", "operator": "GTE", "value": 3}]}},
        )

        assert result.errors and "does not apply" in str(result.errors[0])
