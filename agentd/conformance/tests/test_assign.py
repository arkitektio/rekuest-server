"""Dependent work: an agent assigns a child over its socket, controls it, and asks for it again.

Two raw agents with one ``echo`` action each: the *caller* runs a task assigned through GraphQL
and, as its parent, asks for a child on the *executor*. The caller's identity is also the
GraphQL assigner of the parent, so its socket sees the parent's events too: every wait below is
for frames of the child.
"""

import asyncio
import uuid
from typing import Any

from conftest import ECHO_DECLARATION, RECEIVE_TIMEOUT, session_id


async def caller_and_executor(agents, graphql, caller_token: str, executor_token: str):  # noqa: ANN001, ANN201
    """Both agents registered, the caller running its parent task: (caller, executor, executor id, parent)."""
    caller = await agents()
    caller_init = await caller.register(caller_token, session_id=session_id(), **ECHO_DECLARATION)
    executor = await agents()
    executor_init = await executor.register(executor_token, session_id=session_id(), **ECHO_DECLARATION)
    parent = await graphql(caller_token).assign(caller_init["agent"], "echo", {"x": 1})
    assign = await caller.receive_type("ASSIGN")
    assert assign["task"] == parent
    return caller, executor, executor_init["agent"], parent


def assign_request(parent: str, executor: str, reference: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "ASSIGN_REQUEST",
        "reference": reference,
        "parent": parent,
        "agent": executor,
        "interface": "echo",
        "args": {"x": 2},
    }


async def mirrors_of(agent, task: str, until: str, timeout: float = RECEIVE_TIMEOUT) -> list[dict[str, Any]]:  # noqa: ANN001
    """The ``…_EVENT`` mirrors of ``task`` up to and including the first ``until``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: list[dict[str, Any]] = []
    while True:
        frame = await agent.receive(max(0.01, deadline - loop.time()))
        if frame["type"] == "HEARTBEAT":
            await agent.send({"type": "HEARTBEAT_ANSWER"})
            continue
        if frame["type"].endswith("_EVENT") and frame.get("task") == task:
            seen.append(frame)
            if frame["type"] == until:
                return seen


async def test_an_assign_request_is_answered_and_the_child_delivered(agents, graphql) -> None:  # noqa: ANN001
    caller, executor, executor_id, parent = await caller_and_executor(agents, graphql, "conf_13", "conf_14")
    reference = str(uuid.uuid4())
    request = assign_request(parent, executor_id, reference)
    await caller.send(request)

    response = await caller.receive_type("ASSIGN_RESPONSE")
    assert response["request"] == request["id"], response
    assert response["reference"] == reference, response
    assert response["created"] is True and response.get("error") is None, response
    child = response["task"]
    assert child and child != parent, response

    assign = await executor.receive_type("ASSIGN")
    assert assign["task"] == child, assign
    assert assign["parent"] == parent and assign["root"] == parent, assign
    assert assign["args"] == {"x": 2} and assign["interface"] == "echo", assign
    assert assign["token"], assign


async def test_a_cancel_request_is_answered_and_the_cancel_delivered(agents, graphql) -> None:  # noqa: ANN001
    caller, executor, executor_id, parent = await caller_and_executor(agents, graphql, "conf_15", "conf_16")
    await caller.send(assign_request(parent, executor_id, str(uuid.uuid4())))
    child = (await caller.receive_type("ASSIGN_RESPONSE"))["task"]
    await executor.receive_type("ASSIGN")

    cancel = {"id": str(uuid.uuid4()), "type": "CANCEL_REQUEST", "task": child}
    await caller.send(cancel)
    response = await caller.receive_type("CONTROL_RESPONSE")
    assert response == {**response, "request": cancel["id"], "task": child, "accepted": True}, response
    assert response.get("error") is None, response

    delivered = await executor.receive_type("CANCEL")
    assert delivered["task"] == child, delivered
    cancelling = await mirrors_of(caller, child, "CANCELLING_EVENT")
    assert [m["type"] for m in cancelling] == ["CANCELLING_EVENT"], cancelling


async def test_a_duplicate_request_is_not_created_again_and_replays_the_events(agents, graphql) -> None:  # noqa: ANN001
    caller, executor, executor_id, parent = await caller_and_executor(agents, graphql, "conf_17", "conf_18")
    reference = str(uuid.uuid4())
    await caller.send(assign_request(parent, executor_id, reference))
    child = (await caller.receive_type("ASSIGN_RESPONSE"))["task"]
    await executor.receive_type("ASSIGN")

    await executor.send({"type": "STARTED", "task": child, "seq": 1})
    await executor.receive_type("EVENT_ACK")
    await executor.send({"type": "COMPLETED", "task": child, "seq": 2})
    await executor.receive_type("EVENT_ACK")
    live = await mirrors_of(caller, child, "COMPLETED_EVENT")
    assert [m["type"] for m in live] == ["STARTED_EVENT", "COMPLETED_EVENT"], live

    # Asked again (a workflow resuming): the same child, and its events so far, as the same mirrors.
    again = assign_request(parent, executor_id, reference)
    await caller.send(again)
    response = await caller.receive_type("ASSIGN_RESPONSE")
    assert response["request"] == again["id"], response
    assert (response["task"], response["created"]) == (child, False), response
    replayed = await mirrors_of(caller, child, "COMPLETED_EVENT")
    assert [(m["type"], m["seq"], m["event"]) for m in replayed] == [(m["type"], m["seq"], m["event"]) for m in live], (live, replayed)
