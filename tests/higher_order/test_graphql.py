"""GraphQL surface for higher-order implementations.

``createHigherOrderImplementation`` is served by agentd (``internal/higher-order/create``):
the checks and the writes are judged there (rekuest-agentd ``tests/higher_order.rs``). Here:
that the mutation hands the request over and answers with the row agentd created, that
agentd's refusals reach GraphQL as they are, and that re-registering an agent keeps the
wrappers deployed onto it: agentd's (tests/higher_order.rs), where registration lives.
"""

import json
from collections.abc import Callable

import httpx
import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Release
from kante.context import HttpContext

from facade import agentd
from facade.models import Action, Implementation
from facade.mutations.agent import ImplementAgentInputModel
from facade.schema import schema
from tests.factories import create_agent_for_registry, create_registry_bundle

CREATE = """
    mutation Create($input: CreateHigherOrderImplementationInput!) {
        createHigherOrderImplementation(input: $input) {
            id
            higherOrderConfig
            higherOrderFor { id }
        }
    }
"""

DEFINITION = {"key": "flow_123", "version": "1", "name": "A flow", "kind": "FUNCTION", "args": [{"key": "x", "kind": "INT", "nullable": False}]}


def _build_impls(prefix: str, lower_kind: str = "FUNCTION", higher_kind: str = "FUNCTION") -> tuple[str, str]:
    user, _, org, registry = create_registry_bundle(prefix)
    agent = create_agent_for_registry(registry=registry, user=user, organization=org, prefix=prefix)

    lower_action = Action.objects.create(
        app=agent.app,
        key=f"{prefix}-l",
        version="1.0.0",
        name="l",
        description="l",
        hash=f"{prefix}-l-hash",
        organization=org,
        kind=lower_kind,
    )
    lower = Implementation.objects.create(release=agent.release, interface=f"{prefix}_l", action=lower_action, agent=agent)

    higher_action = Action.objects.create(
        app=agent.app,
        key=f"{prefix}-h",
        version="1.0.0",
        name="h",
        description="h",
        hash=f"{prefix}-h-hash",
        organization=org,
        kind=higher_kind,
    )
    higher = Implementation.objects.create(release=agent.release, interface=f"{prefix}_h", action=higher_action, agent=agent)

    return str(higher.id), str(lower.id)


build_impls = sync_to_async(_build_impls)


def _link(higher_id: str, lower_id: str) -> None:
    Implementation.objects.filter(pk=higher_id).update(higher_order_for_id=lower_id, higher_order_config={"args_key": "args"})


link = sync_to_async(_link)


@pytest.fixture
def agentd_answers(monkeypatch: pytest.MonkeyPatch, settings: object) -> Callable[..., list[dict]]:
    """agentd answered by ``handler``; the JSON bodies it received."""
    settings.AGENTD_URL = "http://agentd:8080/rekuest"
    seen: list[dict] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[dict]:
        def record(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return handler(request)

        monkeypatch.setattr(agentd, "_client", httpx.Client(transport=httpx.MockTransport(record)))
        return seen

    return install


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestCreateHigherOrderImplementation:
    """The mutation hands over to agentd."""

    async def test_the_request_goes_to_agentd_and_the_wrapper_comes_back(self, authenticated_context: HttpContext, agentd_answers: Callable[..., list[dict]]) -> None:
        """The request agentd receives, and the row the mutation answers with."""
        higher_id, lower_id = await build_impls("ho-ok")
        await link(higher_id, lower_id)  # what agentd would have written
        seen = agentd_answers(lambda request: httpx.Response(200, json={"implementation": higher_id, "diagnostics": []}))

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": lower_id, "interface": "flow:123", "definition": DEFINITION, "config": {"args_key": "args"}}},
        )

        assert result.errors is None, result.errors
        payload = result.data["createHigherOrderImplementation"]
        assert payload == {"id": higher_id, "higherOrderConfig": {"args_key": "args"}, "higherOrderFor": {"id": lower_id}}
        (body,) = seen
        assert body["input"]["lower"] == lower_id and body["input"]["interface"] == "flow:123"
        assert body["input"]["definition"]["key"] == "flow_123"
        assert body["input"]["config"] == {"args_key": "args"}
        assert body["principal"]["organization"] is not None

    async def test_a_refusal_reaches_graphql_as_agentd_worded_it(self, authenticated_context: HttpContext, agentd_answers: Callable[..., list[dict]]) -> None:
        """agentd's message is the GraphQL error."""
        agentd_answers(lambda request: httpx.Response(400, json={"error": "An implementation cannot wrap itself"}))

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": "1", "interface": "flow:1", "definition": DEFINITION}},
        )

        assert result.errors is not None and result.errors[0].message == "An implementation cannot wrap itself"

    async def test_without_agentd_it_says_so(self, authenticated_context: HttpContext, settings: object) -> None:
        """No agentd configured: a clear error, no in-process fallback."""
        settings.AGENTD_URL = None

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": "1", "interface": "flow:1", "definition": DEFINITION}},
        )

        assert result.errors is not None and "agentd" in result.errors[0].message
