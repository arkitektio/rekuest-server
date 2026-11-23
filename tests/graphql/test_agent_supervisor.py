"""An agent's supervisor (the app that provisioned its client): the ``supervisor`` field, and the filters pickers use.

``@rekuest/agent`` carries one descriptor, ``@rekuest/supervisor``. A picker hands a port's
constraints in as ``requires``; any other key is refused, never ignored.
"""

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from authentikate.models import App, Client, Device, Organization, Release, User
from django.utils import timezone
from kante.context import HttpContext

from facade.models import Agent
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

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
    # takt writes the supervisor from the token's `act` claim; here the row is made directly.
    agent = Agent.objects.create(
        app=app,
        release=release,
        user=request.user,
        client=client,
        organization=request.organization,
        hash=f"{name}-hash",
        name=name,
        supervisor=supervisor,
    )
    return str(agent.id)


_agent = sync_to_async(_make_agent)


async def _ids(context: HttpContext, filters: dict) -> set[str]:
    result = await schema.execute(
        AGENTS, context_value=context, variable_values={"filters": filters}
    )
    assert result.data is not None, result.errors
    return {agent["id"] for agent in result.data["agents"]}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentSupervisor:
    async def test_the_agent_shows_its_supervisor(
        self, authenticated_context: HttpContext
    ) -> None:
        managed = await _agent(authenticated_context, "managed", "plugin_engine")

        result = await schema.execute(
            AGENTS,
            context_value=authenticated_context,
            variable_values={"filters": {"ids": [managed]}},
        )

        assert result.data is not None, result.errors
        assert result.data["agents"] == [{"id": managed, "supervisor": "plugin_engine"}]

    async def test_requires_narrows_by_the_supervisor(
        self, authenticated_context: HttpContext
    ) -> None:
        managed = await _agent(authenticated_context, "managed", "plugin_engine")
        elsewhere = await _agent(authenticated_context, "elsewhere", "local_launcher")
        free = await _agent(authenticated_context, "free", None)
        mine = {managed, elsewhere, free}

        def requires(operator: str, value: object = None) -> dict:
            return {
                "requires": [
                    {"key": "@rekuest/supervisor", "operator": operator, "value": value}
                ]
            }

        assert await _ids(
            authenticated_context, requires("EQUALS", "plugin_engine")
        ) & mine == {managed}
        assert await _ids(
            authenticated_context, requires("MATCHES", "plugin_engine")
        ) & mine == {managed}
        assert await _ids(
            authenticated_context, requires("IN", ["plugin_engine", "local_launcher"])
        ) & mine == {managed, elsewhere}
        assert await _ids(authenticated_context, requires("EXISTS")) & mine == {
            managed,
            elsewhere,
        }
        # An agent that declares none has no value: only the negations hold for it.
        assert await _ids(
            authenticated_context, requires("NOT_EQUALS", "plugin_engine")
        ) & mine == {elsewhere, free}
        assert await _ids(
            authenticated_context, requires("NOT_IN", ["plugin_engine"])
        ) & mine == {elsewhere, free}
        assert await _ids(
            authenticated_context, {"supervisor": "local_launcher"}
        ) & mine == {elsewhere}

    async def test_supervised_splits_agents_by_whether_they_declared_one(
        self, authenticated_context: HttpContext
    ) -> None:
        managed = await _agent(authenticated_context, "managed", "plugin_engine")
        elsewhere = await _agent(authenticated_context, "elsewhere", "local_launcher")
        free = await _agent(authenticated_context, "free", None)
        mine = {managed, elsewhere, free}

        assert await _ids(authenticated_context, {"supervised": True}) & mine == {
            managed,
            elsewhere,
        }
        assert await _ids(authenticated_context, {"supervised": False}) & mine == {free}

    async def test_a_key_the_agent_does_not_carry_is_refused(
        self, authenticated_context: HttpContext
    ) -> None:
        await _agent(authenticated_context, "managed", "plugin_engine")

        result = await schema.execute(
            AGENTS,
            context_value=authenticated_context,
            variable_values={
                "filters": {
                    "requires": [
                        {"key": "@mikro/n_channels", "operator": "EQUALS", "value": 3}
                    ]
                }
            },
        )

        assert result.errors and "declares no descriptor" in str(result.errors[0])

    async def test_an_operator_a_string_cannot_take_is_refused(
        self, authenticated_context: HttpContext
    ) -> None:
        result = await schema.execute(
            AGENTS,
            context_value=authenticated_context,
            variable_values={
                "filters": {
                    "requires": [
                        {"key": "@rekuest/supervisor", "operator": "GTE", "value": 3}
                    ]
                }
            },
        )

        assert result.errors and "does not apply" in str(result.errors[0])


SUPERVISORS = """
    query Supervisors($filters: AgentFilter) {
        supervisors(filters: $filters) {
            identifier
            agentCount
            connectedCount
        }
    }
"""


def _agent_in(
    organization: Organization,
    user: User,
    name: str,
    supervisor: str | None,
    *,
    connected: bool = False,
) -> None:
    device = Device.objects.create(device_id=f"{name}-device")
    client = Client.objects.create(client_id=f"{name}-client", device=device)
    app = App.objects.create(identifier=f"{name}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    Agent.objects.create(
        app=app,
        release=release,
        user=user,
        client=client,
        organization=organization,
        hash=f"{name}-hash",
        name=name,
        supervisor=supervisor,
        connected=connected,
        last_seen=timezone.now() if connected else None,
    )


@pytest.mark.django_db(transaction=True)
class TestSupervisors:
    def test_the_organizations_supervisors_with_their_agents_counted(self) -> None:
        context, user, _, organization = tenant_context(TEST_TOKEN)
        _, other_user, _, other_organization = tenant_context(OTHER_TOKEN)
        _agent_in(organization, user, "one", "plugin_engine", connected=True)
        _agent_in(organization, user, "two", "plugin_engine")
        _agent_in(organization, user, "three", "local_launcher")
        _agent_in(organization, user, "free", None, connected=True)
        _agent_in(other_organization, other_user, "theirs", "their_engine")

        result = async_to_sync(schema.execute)(SUPERVISORS, context_value=context)

        assert result.errors is None, result.errors
        assert result.data == {
            "supervisors": [
                {"identifier": "local_launcher", "agentCount": 1, "connectedCount": 0},
                {"identifier": "plugin_engine", "agentCount": 2, "connectedCount": 1},
            ]
        }

    def test_filters_narrow_the_agents_counted(self) -> None:
        context, user, _, organization = tenant_context(TEST_TOKEN)
        _agent_in(organization, user, "one", "plugin_engine", connected=True)
        _agent_in(organization, user, "two", "plugin_engine")
        _agent_in(organization, user, "three", "local_launcher")

        result = async_to_sync(schema.execute)(
            SUPERVISORS,
            context_value=context,
            variable_values={"filters": {"connected": True}},
        )

        assert result.errors is None, result.errors
        assert result.data == {
            "supervisors": [
                {"identifier": "plugin_engine", "agentCount": 1, "connectedCount": 1}
            ]
        }
