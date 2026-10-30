"""Filtering actions and implementations by what their tasks may do: ask, inform, call, or any of
it at run time (``facade/filters/capabilities.py``).

Declared asks and informs are written by takt at registration; here they are rows built directly
(``ImplementationInteraction`` on a blok of the organization), like the dependencies
``create_implementation`` writes.
"""

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from authentikate.models import App, Client, Release
from rekuest_core.inputs.models import ImplementationInputModel

from facade import models
from facade.schema import schema
from tests.registered import create_blok, create_implementation

ACTIONS = """
    query Actions($filters: ActionFilter) {
        actions(filters: $filters, pagination: { limit: 100 }) {
            name
        }
    }
"""

IMPLEMENTATIONS = """
    query Implementations($filters: ImplementationFilter) {
        implementations(filters: $filters, pagination: { limit: 100 }) {
            interface
        }
    }
"""

SEEDED = {"Asker", "Informer", "Caller", "DynamicCaller", "DynamicAsker", "Plain", "Twice"}


def _input(name: str, dependencies: list | None = None) -> ImplementationInputModel:
    return ImplementationInputModel.model_validate(
        {
            "interface": name.lower(),
            "dependencies": dependencies or [],
            "definition": {"key": name.lower(), "version": "1", "name": name, "kind": "FUNCTION", "args": [], "returns": []},
        }
    )


def _seed(context: HttpContext) -> None:
    request = context.request
    org = request.organization
    app = App.objects.create(identifier="capability-app")
    release = Release.objects.create(app=app, version="1.0.0")

    def agent(name: str, client: Client) -> models.Agent:
        return models.Agent.objects.create(app=app, release=release, user=request.user, client=client, organization=org, hash=f"capability-{name}", name=name)

    # One agent per client, user and organization: the second runs on a client of its own.
    first = agent("first", request.client)
    second = agent("second", Client.objects.create(client_id="capability-second", release=release))
    blok = create_blok(org, request.user, "question")

    def declare(implementation: models.Implementation, role: models.InteractionRole) -> None:
        models.ImplementationInteraction.objects.create(implementation=implementation, key=f"ui.{role.value.lower()}", role=role, blok=blok)

    declare(create_implementation(_input("Asker"), first), models.InteractionRole.ASK)
    declare(create_implementation(_input("Informer"), first), models.InteractionRole.INFORM)
    create_implementation(_input("Caller", [{"key": "segment", "action_dependencies": [], "state_dependencies": []}]), first)
    dynamic_caller = create_implementation(_input("DynamicCaller"), first)
    models.Implementation.objects.filter(id=dynamic_caller.id).update(dynamic_calls=True)
    dynamic_asker = create_implementation(_input("DynamicAsker"), first)
    models.Implementation.objects.filter(id=dynamic_asker.id).update(dynamic_asks=True)
    create_implementation(_input("Plain"), first)
    # The same action implemented twice, both asking: listed once.
    declare(create_implementation(_input("Twice"), first), models.InteractionRole.ASK)
    declare(create_implementation(_input("Twice"), second), models.InteractionRole.ASK)


async def _names(context: HttpContext, filters: dict) -> list[str]:
    result = await schema.execute(ACTIONS, context_value=context, variable_values={"filters": filters})
    assert result.errors is None, result.errors
    return [action["name"] for action in result.data["actions"] if action["name"] in SEEDED]


@pytest_asyncio.fixture
async def seeded(authenticated_context: HttpContext) -> HttpContext:
    # The auth extension sets the request's organization during the first execute: seed into it.
    await schema.execute("query { __typename }", context_value=authenticated_context)
    await sync_to_async(_seed)(authenticated_context)
    return authenticated_context


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestActionCapabilityFilters:
    async def test_asks_counts_declared_and_dynamic_asks(self, seeded: HttpContext):
        assert sorted(await _names(seeded, {"asks": True})) == ["Asker", "DynamicAsker", "Twice"]

    async def test_asks_false_is_never_asks(self, seeded: HttpContext):
        assert sorted(await _names(seeded, {"asks": False})) == ["Caller", "DynamicCaller", "Informer", "Plain"]

    async def test_informs(self, seeded: HttpContext):
        assert await _names(seeded, {"informs": True}) == ["Informer"]

    async def test_calls_counts_dependencies_and_dynamic_calls(self, seeded: HttpContext):
        assert sorted(await _names(seeded, {"calls": True})) == ["Caller", "DynamicCaller"]

    async def test_dynamic_flags(self, seeded: HttpContext):
        assert await _names(seeded, {"dynamicCalls": True}) == ["DynamicCaller"]
        assert await _names(seeded, {"dynamicAsks": True}) == ["DynamicAsker"]
        assert await _names(seeded, {"dynamicInforms": True}) == []

    async def test_compose_under_or_and_not(self, seeded: HttpContext):
        assert sorted(await _names(seeded, {"informs": True, "OR": {"calls": True}})) == ["Caller", "DynamicCaller", "Informer"]
        assert sorted(await _names(seeded, {"NOT": {"asks": True}, "calls": False})) == ["Informer", "Plain"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestImplementationCapabilityFilters:
    async def test_asks_lists_each_implementation(self, seeded: HttpContext):
        result = await schema.execute(IMPLEMENTATIONS, context_value=seeded, variable_values={"filters": {"asks": True}})
        assert result.errors is None, result.errors
        assert sorted(i["interface"] for i in result.data["implementations"]) == ["asker", "dynamicasker", "twice", "twice"]

    async def test_calls_false(self, seeded: HttpContext):
        result = await schema.execute(IMPLEMENTATIONS, context_value=seeded, variable_values={"filters": {"calls": False, "asks": False}})
        assert result.errors is None, result.errors
        assert sorted(i["interface"] for i in result.data["implementations"]) == ["informer", "plain"]
