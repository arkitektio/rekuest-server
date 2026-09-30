"""Locks are visible (who holds them, who needs them), and test cases stay in their organization.

Locks were written by agents and read by nobody; a test case could be declared on another
organization's actions, and a result's payload was stored but never shown.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade import models
from facade.schema import schema
from tests.factories import create_action_for_organization, create_agent_for_registry, create_registry_bundle


def _agent_in(context: HttpContext, prefix: str) -> models.Agent:
    request = context.request
    caller, _ = models.Caller.objects.get_or_create(client=request.client, user=request.user, organization=request.organization)
    return create_agent_for_registry(caller, request.user, request.organization, prefix)


def _locked(context: HttpContext) -> tuple[models.Agent, models.Task]:
    agent = _agent_in(context, "locks")
    action = create_action_for_organization(agent.organization, "locks-action")
    implementation = models.Implementation.objects.create(agent=agent, action=action, interface="move", release=agent.release)
    lock = models.Lock.objects.create(agent=agent, key="stage", description="the stage")
    implementation.required_locks.add(lock)
    task = models.Task.objects.create(action=action, implementation=implementation, agent=agent, args={}, latest_event_kind="STARTED", latest_instruct_kind="ASSIGN")
    lock.hold_by = task
    lock.save()
    return agent, task


LOCKS = """
    query ($agent: ID!, $task: ID!) {
        agent(id: $agent) { locks { key description heldBy { id } requiredBy { interface } } }
        task(id: $task) { heldLocks { key } }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_lock_shows_its_holder_and_who_requires_it(authenticated_context: HttpContext) -> None:
    """An agent's locks, the task holding one, and the implementations that take it."""
    agent, task = await sync_to_async(_locked)(authenticated_context)

    result = await schema.execute(LOCKS, context_value=authenticated_context, variable_values={"agent": str(agent.pk), "task": str(task.pk)})

    assert result.errors is None, result.errors
    assert result.data["agent"]["locks"] == [{"key": "stage", "description": "the stage", "heldBy": {"id": str(task.pk)}, "requiredBy": [{"interface": "move"}]}]
    assert result.data["task"]["heldLocks"] == [{"key": "stage"}]


CREATE_CASE = """
    mutation ($action: ID!, $tester: ID!) {
        createTestCase(input: {action: $action, tester: $tester, isBenchmark: true}) { id isBenchmark }
    }
"""
CREATE_RESULT = """
    mutation ($case: ID!, $impl: ID!, $result: AnyDefault) {
        createTestResult(input: {case: $case, tester: $impl, implementation: $impl, passed: true, result: $result}) { id result }
    }
"""
RESULTS = """
    query ($action: ID!) { testResults(filters: {action: $action}) { passed result } }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_test_cases_stay_in_their_organization_and_results_keep_their_payload(authenticated_context: HttpContext) -> None:
    """A test case on another organization's action is refused; a result's JSON is shown and filterable by action."""

    @sync_to_async
    def seed():
        agent = _agent_in(authenticated_context, "cases")
        action = create_action_for_organization(agent.organization, "cases-target")
        tester = create_action_for_organization(agent.organization, "cases-tester")
        implementation = models.Implementation.objects.create(agent=agent, action=action, interface="run", release=agent.release)
        _, _, other_org, _ = create_registry_bundle("cases-other")
        foreign = create_action_for_organization(other_org, "cases-foreign")
        return action, tester, implementation, foreign

    action, tester, implementation, foreign = await seed()

    refused = await schema.execute(CREATE_CASE, context_value=authenticated_context, variable_values={"action": str(foreign.pk), "tester": str(tester.pk)})
    assert refused.errors is not None

    created = await schema.execute(CREATE_CASE, context_value=authenticated_context, variable_values={"action": str(action.pk), "tester": str(tester.pk)})
    assert created.errors is None, created.errors
    assert created.data["createTestCase"]["isBenchmark"] is True

    recorded = await schema.execute(CREATE_RESULT, context_value=authenticated_context, variable_values={"case": created.data["createTestCase"]["id"], "impl": str(implementation.pk), "result": {"ms": 12}})
    assert recorded.errors is None, recorded.errors
    assert recorded.data["createTestResult"]["result"] == {"ms": 12}

    listed = await schema.execute(RESULTS, context_value=authenticated_context, variable_values={"action": str(action.pk)})
    assert listed.errors is None, listed.errors
    assert listed.data["testResults"] == [{"passed": True, "result": {"ms": 12}}]
