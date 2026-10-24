"""Held work, as GraphQL reads and answers it: a root's ``policy``, a child an agent asks for
waiting as HELD, and ``decide``.

takt holds and releases (its ``tests/holds.rs`` covers every rule); this pins the server's half:
``AssignInput.policy`` reaches takt, ``Task.policy`` and the events' ``value`` read back, and a
decision goes to takt and its refusal comes back as GraphQL errors.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Release
from django.utils import timezone
from kante.context import HttpContext

from facade import models, takt_api
from facade import takt as takt_client
from facade.inputs import AssignInputModel
from facade.schema import schema
from rekuest_core.inputs.models import ImplementationInputModel
from tests.factories import _build_task, create_action_for_organization, create_registry_bundle
from tests.registered import create_implementation

ASSIGN = """
    mutation Assign($input: AssignInput!) {
        assign(input: $input) { id policy latestEventKind }
    }
"""

DECIDE = """
    mutation Decide($input: DecideInput!) {
        decide(input: $input) { id latestEventKind isDone events { kind message value } }
    }
"""

TASK = "query Task($id: ID!) { task(id: $id) { id policy latestEventKind events { kind message value } } }"


def _app_agent(context: HttpContext, name: str) -> models.Agent:
    """A live app of the request's user, on a client of its own (an app is never the UI)."""
    request = context.request
    device = Device.objects.create(device_id=f"{name}-device")
    client = Client.objects.create(client_id=f"{name}-client", device=device)
    app = App.objects.create(identifier=f"{name}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = models.Agent.objects.create(
        app=app, release=release, user=request.user, client=client, organization=request.organization, hash=f"{name}-hash", name=name, connected=True, last_seen=timezone.now()
    )
    create_implementation(
        ImplementationInputModel.model_validate({"interface": "work", "definition": {"key": "work", "version": "1", "name": f"{name} work", "kind": "FUNCTION", "args": [], "returns": []}}),
        agent,
    )
    return agent


async def _execute(context: HttpContext, query: str, variables: dict) -> dict:
    result = await schema.execute(query, context_value=context, variable_values=variables)
    assert result.errors is None, result.errors
    return result.data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_root_asks_unless_its_assign_says_auto(authenticated_context: HttpContext, takt: str) -> None:
    await schema.execute("query { __typename }", context_value=authenticated_context)
    agent = await sync_to_async(_app_agent)(authenticated_context, "policy-root")
    implementation = await models.Implementation.objects.aget(agent=agent, interface="work")

    unsaid = await _execute(authenticated_context, ASSIGN, {"input": {"implementation": str(implementation.pk), "args": {}}})
    assert unsaid["assign"]["policy"] == "ASK"
    auto = await _execute(authenticated_context, ASSIGN, {"input": {"implementation": str(implementation.pk), "args": {}, "policy": "AUTO"}})
    assert (auto["assign"]["policy"], auto["assign"]["latestEventKind"]) == ("AUTO", "QUEUED")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_held_child_is_decided_by_its_person(authenticated_context: HttpContext, takt: str) -> None:
    await schema.execute("query { __typename }", context_value=authenticated_context)
    asker = await sync_to_async(_app_agent)(authenticated_context, "hold-asker")
    runner = await sync_to_async(_app_agent)(authenticated_context, "hold-runner")
    workflow = await models.Implementation.objects.aget(agent=asker, interface="work")
    work = await models.Implementation.objects.aget(agent=runner, interface="work")

    root = (await _execute(authenticated_context, ASSIGN, {"input": {"implementation": str(workflow.pk), "args": {}}}))["assign"]["id"]
    # The asker asks for a child, as its socket would; takt holds it.
    as_the_asker = takt_api.Principal(user=asker.user_id, client=asker.client_id, organization=asker.organization_id)
    answer = await sync_to_async(takt_client.call)(
        takt_api.ASSIGN,
        takt_api.AssignRequest(principal=as_the_asker, input=AssignInputModel(implementation=str(work.pk), args={}, parent=root)),
    )
    child = answer.task

    held = (await _execute(authenticated_context, TASK, {"id": child}))["task"]
    assert (held["policy"], held["latestEventKind"]) == ("ASK", "HELD")
    (event,) = held["events"]
    assert event["kind"] == "HELD"
    assert event["message"] == "hold-asker wants to run hold-runner work"
    assert (event["value"]["kind"], event["value"]["caller_task"], event["value"]["agent"]["id"]) == ("assign", root, str(runner.pk))
    root_events = (await _execute(authenticated_context, TASK, {"id": root}))["task"]["events"]
    assert [(e["kind"], e["value"]["held"]) for e in root_events] == [("HELD", child)]

    denied = (await _execute(authenticated_context, DECIDE, {"input": {"task": child, "approved": False, "answer": "not this one"}}))["decide"]
    assert (denied["latestEventKind"], denied["isDone"]) == ("DENIED", True)
    assert [e["kind"] for e in denied["events"]] == ["HELD", "DECIDED", "DENIED"]
    decided = denied["events"][1]["value"]
    assert (decided["approved"], decided["answer"], decided["auto"]) == (False, "not this one", False)

    again = await schema.execute(DECIDE, context_value=authenticated_context, variable_values={"input": {"task": child, "approved": True}})
    assert again.errors and "terminal" in again.errors[0].message


@pytest.mark.django_db(transaction=True)
def test_a_new_task_or_rule_asks() -> None:
    """A row made now asks, whoever writes it."""
    _, _, organization, caller = create_registry_bundle("policy-rules")
    action = create_action_for_organization(organization, "policy-rules")
    assert models.Schedule.objects.create(name="new", caller=caller, action=action, interval_seconds=60).policy == "ASK"
    assert _build_task("policy-after").policy == "ASK"
    after = _build_task("policy-after-db")
    assert models.Task.objects.get(pk=after.pk).policy == "ASK"
