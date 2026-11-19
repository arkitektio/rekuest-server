"""A task is part of the trace it was assigned in, through the real takt (which records here).

The request that assigns arrives at takt inside a trace; takt's own ``assign`` span continues
it, the task remembers that place (``Task.traceparent``), and the ``ASSIGN`` its agent is sent
says so. A child continues where its parent was assigned. A task assigned in no trace starts
one of its own, since takt records.
"""

import json
import os
import secrets
import time

import httpx
import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext
from opentelemetry import context as otel_context
from opentelemetry import propagate

from facade import models, takt_api
from facade import takt as takt_client
from facade.inputs import AssignInputModel
from facade.schema import schema
from tests.graphql.test_holds import ASSIGN, _app_agent, _execute


def _a_trace() -> tuple[str, str]:
    """A trace somebody else started: its id, and the header a request inside it carries."""
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


def _spans(trace_id: str, at_least: int) -> list[dict]:
    """The trace's spans as jaeger has them, once there are ``at_least``."""
    url = f"http://localhost:{os.environ['REKUEST_TEST_JAEGER_PORT']}/api/traces/{trace_id}"
    deadline = time.monotonic() + 30
    found: list[dict] = []
    while time.monotonic() < deadline:
        response = httpx.get(url, timeout=5)
        if response.status_code == 200:
            trace = response.json()["data"][0]
            found = [{**span, "service": trace["processes"][span["processID"]]["serviceName"]} for span in trace["spans"]]
            if len(found) >= at_least:
                break
        time.sleep(0.3)
    assert len(found) >= at_least, f"jaeger holds {[span['operationName'] for span in found]} of trace {trace_id}"
    return found


def _named(spans: list[dict], name: str) -> dict:
    (span,) = [span for span in spans if span["operationName"] == name]
    return span


def _parent(span: dict) -> str | None:
    return next((reference["spanID"] for reference in span["references"] if reference["refType"] == "CHILD_OF"), None)


def _sent(task: str) -> dict:
    """The ``ASSIGN`` takt queued for the task's agent."""
    command = models.Command.objects.get(task=int(task), kind="ASSIGN")
    return json.loads(command.frame)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_task_continues_the_trace_it_was_assigned_in(authenticated_context: HttpContext, takt: str) -> None:
    await schema.execute("query { __typename }", context_value=authenticated_context)
    agent = await sync_to_async(_app_agent)(authenticated_context, "traced")
    implementation = await models.Implementation.objects.aget(agent=agent, interface="work")
    trace_id, traceparent = _a_trace()

    # As inside a traced request: what this server asks of takt carries the trace on.
    entered = otel_context.attach(propagate.extract({"traceparent": traceparent}))
    try:
        root = (await _execute(authenticated_context, ASSIGN, {"input": {"implementation": str(implementation.pk), "args": {}, "policy": "AUTO"}}))["assign"]["id"]
    finally:
        otel_context.detach(entered)

    task = await models.Task.objects.aget(pk=root)
    version, of_trace, of_span, flags = task.traceparent.split("-")
    assert (version, of_trace, flags) == ("00", trace_id, "01")
    assert (await sync_to_async(_sent)(root))["traceparent"] == task.traceparent, "the agent is told the same place"

    spans = await sync_to_async(_spans)(trace_id, 2)
    request, assign = _named(spans, "POST /internal/assign"), _named(spans, "assign")
    assert {request["service"], assign["service"]} == {"rekuest-takt"}
    assert _parent(request) == traceparent.split("-")[2], "takt's request continues the trace it arrived in"
    assert _parent(assign) == request["spanID"]
    assert assign["spanID"] == of_span, "the task continues from takt's assign"

    # A child asked for in no trace of its own (as an agent that does not trace asks) continues
    # where its parent was assigned.
    as_the_agent = takt_api.Principal(user=agent.user_id, client=agent.client_id, organization=agent.organization_id)
    answer = await sync_to_async(takt_client.call)(
        takt_api.ASSIGN,
        takt_api.AssignRequest(principal=as_the_agent, input=AssignInputModel(implementation=str(implementation.pk), args={}, parent=root)),
    )
    child = await models.Task.objects.aget(pk=answer.task)
    assert child.traceparent.split("-")[1] == trace_id
    spans = await sync_to_async(_spans)(trace_id, 3)
    (child_assign,) = [span for span in spans if span["spanID"] == child.traceparent.split("-")[2]]
    assert _parent(child_assign) == of_span, "the child's assign is under its parent's"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_task_assigned_in_no_trace_starts_one(authenticated_context: HttpContext, takt: str) -> None:
    await schema.execute("query { __typename }", context_value=authenticated_context)
    agent = await sync_to_async(_app_agent)(authenticated_context, "untraced")
    implementation = await models.Implementation.objects.aget(agent=agent, interface="work")

    root = (await _execute(authenticated_context, ASSIGN, {"input": {"implementation": str(implementation.pk), "args": {}, "policy": "AUTO"}}))["assign"]["id"]

    task = await models.Task.objects.aget(pk=root)
    assert task.traceparent is not None and task.traceparent.endswith("-01")
    spans = await sync_to_async(_spans)(task.traceparent.split("-")[1], 2)
    assert _parent(_named(spans, "POST /internal/assign")) is None, "the root of a trace of takt's own"
