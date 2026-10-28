"""What an app may do here is what its token's scopes say: every root field asks for one of rekuest's
own (the ``@auth`` directive publishes it, a generated client gathers it), and `rekuest_all` stands
for all of them. What a member may do is what their roles say: writes ask for `rekuest_editor`,
automations for `rekuest_automator`, and `rekuest_admin` holds both."""

import pytest
from arkitekt_service.graphql import scope_gaps
from dokker import Deployment
from graphql import ExecutionResult
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from facade.schema import schema

MAKE_DASHBOARD = 'mutation { createDashboard(input: {name: "scoped"}) { id } }'
DASHBOARDS = "query { dashboards { id } }"
REGISTER_MODULE = 'mutation { registerModule(input: {name: "scoped"}) { id } }'
# Refused at the gate, before the resolver could look the schedule up.
DELETE_SCHEDULE = 'mutation { deleteSchedule(input: {id: "1"}) }'


def _as(token: str) -> HttpContext:
    return HttpContext(request=UniversalRequest(_extensions={}), response=TemporalResponse(), headers={"Authorization": f"Bearer {token}"}, type="http")


def _reason(result: ExecutionResult) -> str | None:
    return result.errors[0].extensions.get("reason") if result.errors else None


def test_every_root_field_asks_for_a_scope_rekuest_declares() -> None:
    """No root field is left to authentication alone, or asks for a scope rekuest does not declare."""
    assert scope_gaps(schema) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_app_granted_reads_reads_and_writes_nothing(backend_stack: Deployment) -> None:
    """`rekuest_read` reads, and is refused a write for its scope, whatever the member's roles."""
    assert (await schema.execute(DASHBOARDS, context_value=_as("readertest"))).errors is None
    refused = await schema.execute(MAKE_DASHBOARD, context_value=_as("readertest"))
    assert _reason(refused) == "INSUFFICIENT_SCOPE"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_all_scope_writes(backend_stack: Deployment) -> None:
    """`rekuest_all` passes a field that asks for `rekuest_write`."""
    made = await schema.execute(MAKE_DASHBOARD, context_value=_as("test"))
    assert made.errors is None, made.errors


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_agent_registers_and_builds_no_dashboards(backend_stack: Deployment) -> None:
    """What the plugin engine is granted (`rekuest_read rekuest_agent`) is enough to be an agent."""
    registered = await schema.execute(REGISTER_MODULE, context_value=_as("agenttest"))
    assert registered.errors is None, registered.errors
    refused = await schema.execute(MAKE_DASHBOARD, context_value=_as("agenttest"))
    assert _reason(refused) == "INSUFFICIENT_SCOPE"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_editor_sets_up_no_automations(backend_stack: Deployment) -> None:
    """Every member is an editor; only an automator (or rekuest's admin) sets up what runs on its own."""
    refused = await schema.execute(DELETE_SCHEDULE, context_value=_as("test-member"))
    assert _reason(refused) == "INSUFFICIENT_ROLE"
