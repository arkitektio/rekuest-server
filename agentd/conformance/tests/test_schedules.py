"""Schedules across the pair: the server owns the row, agentd plans and runs it."""

import uuid

from conftest import ECHO_DECLARATION, session_id

ACTION = "query ($id: ID!) { agent(id: $id) { implementations { interface action { id } } } }"
CREATE = """
    mutation ($input: CreateScheduleInput!) {
        createSchedule(input: $input) { id nextRun { id notBefore latestEventKind } }
    }
"""
RUN_NOW = "mutation ($id: ID!) { triggerSchedule(input: {id: $id}) { id } }"
DELETE = "mutation ($id: ID!) { deleteSchedule(input: {id: $id}) }"


async def test_a_schedule_is_planned_run_now_and_refused_when_its_timing_is_not_one(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_33", session_id=session_id(), hash=f"scheduled-{uuid.uuid4().hex}", **ECHO_DECLARATION)
    api = graphql("conf_33")
    (implementation,) = (await api(ACTION, id=init["agent"]))["agent"]["implementations"]
    schedule = {"name": "nightly", "action": implementation["action"]["id"], "agent": init["agent"], "interface": "echo", "args": {"x": 4}}

    created = (await api(CREATE, input={**schedule, "cron": "0 2 * * *", "timezone": "Europe/Berlin"}))["createSchedule"]
    waiting = created["nextRun"]
    assert waiting["latestEventKind"] == "QUEUED" and waiting["notBefore"], "agentd planned the next 02:00 in Berlin at once"

    # Run now: the waiting run itself is moved to now, and agentd's reaper dispatches it.
    assert (await api(RUN_NOW, id=created["id"]))["triggerSchedule"]["id"] == waiting["id"]
    assign = await agent.receive_type("ASSIGN")
    assert (assign["task"], assign["args"]) == (waiting["id"], {"x": 4})

    # agentd is the one reader of cron lines: its refusal is the mutation's error.
    refused = await api.client.post(api.url, json={"query": CREATE, "variables": {"input": {**schedule, "cron": "whenever"}}}, headers={"Authorization": f"Bearer {api.token}"})
    (error,) = refused.json()["errors"]
    assert 'Not a valid cron line: "whenever"' in error["message"]

    assert (await api(DELETE, id=created["id"]))["deleteSchedule"] == created["id"]

