"""GraphQL surface for higher-order implementations.

``createHigherOrderImplementation`` is served by takt (``internal/higher-order/create``):
the checks and the writes are judged there (rekuest-takt ``tests/higher_order.rs``). Here:
that the mutation hands the request to the real takt and answers with the row it created,
that takt's refusals reach GraphQL as they are, and that re-registering an agent keeps the
wrappers deployed onto it: takt's (tests/higher_order.rs), where registration lives.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import Organization
from kante.context import HttpContext

from facade.models import Action, Implementation
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


def _build_impls(prefix: str, lower_kind: str = "FUNCTION", higher_kind: str = "FUNCTION", organization: Organization | None = None) -> tuple[str, str]:
    user, _, org, registry = create_registry_bundle(prefix)
    org = organization or org
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
    lower = Implementation.objects.create(interface=f"{prefix}_l", action=lower_action, agent=agent)

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
    higher = Implementation.objects.create(interface=f"{prefix}_h", action=higher_action, agent=agent)

    return str(higher.id), str(lower.id)


build_impls = sync_to_async(_build_impls)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.usefixtures("takt")
class TestCreateHigherOrderImplementation:
    """The mutation hands over to takt, the real one."""

    async def test_takt_deploys_the_wrapper_and_it_comes_back(self, authenticated_context: HttpContext) -> None:
        """The wrapper takt registered on the lower implementation's agent is what the mutation answers with."""
        _, lower_id = await build_impls("ho-ok", organization=authenticated_context.request.organization)

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": lower_id, "interface": "flow:123", "definition": DEFINITION, "config": {"args_key": "args"}}},
        )

        assert result.errors is None, result.errors
        payload = result.data["createHigherOrderImplementation"]
        assert (payload["higherOrderConfig"], payload["higherOrderFor"]) == ({"args_key": "args"}, {"id": lower_id})
        wrapper = await Implementation.objects.select_related("action").aget(pk=payload["id"])
        lower = await Implementation.objects.aget(pk=lower_id)
        assert (wrapper.interface, wrapper.action.key, wrapper.agent_id) == ("flow:123", "flow_123", lower.agent_id)

    async def test_a_refusal_reaches_graphql_as_takt_worded_it(self, authenticated_context: HttpContext) -> None:
        """takt's message is the GraphQL error: here, an implementation of another organization."""
        _, foreign = await build_impls("ho-foreign")

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": foreign, "interface": "flow:1", "definition": DEFINITION}},
        )

        assert result.errors is not None and result.errors[0].message
        assert "takt" not in result.errors[0].message  # the refusal itself, not "takt is unavailable"

    async def test_without_takt_it_says_so(self, authenticated_context: HttpContext, settings: object) -> None:
        """No takt configured: a clear error, no in-process fallback."""
        settings.TAKT_URL = None

        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"lower": "1", "interface": "flow:1", "definition": DEFINITION}},
        )

        assert result.errors is not None and "takt" in result.errors[0].message
