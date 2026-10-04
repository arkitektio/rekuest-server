"""Schedules across the pair: the server owns the row, takt plans and runs it."""

import asyncio
import uuid

from conftest import ECHO_DECLARATION, session_id

ACTION = "query ($id: ID!) { agent(id: $id) { implementations { interface action { id } } } }"
CREATE = "mutation ($input: CreateScheduleInput!) { createSchedule(input: $input) { id } }"
READ = "query ($id: ID!) { schedule(id: $id) { id nextRun { id notBefore latestEventKind } upcoming(count: 2) } }"
LIST = "query { schedules { id upcoming(count: 2) } }"
RUN_NOW = "mutation ($id: ID!) { triggerSchedule(input: {id: $id}) { id } }"
DELETE = "mutation ($id: ID!) { deleteSchedule(input: {id: $id}) }"


async def waiting_run(api, schedule: str) -> dict | None:  # noqa: ANN001
    """The schedule's next run, once takt planned it (it hears of the row when it commits)."""
    for _ in range(40):
        run = (await api(READ, id=schedule))["schedule"]["nextRun"]
        if run is not None:
            return run
        await asyncio.sleep(0.05)
    return None


async def test_a_schedule_is_planned_run_now_and_refused_when_its_timing_is_not_one(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_33", session_id=session_id(), hash=f"scheduled-{uuid.uuid4().hex}", **ECHO_DECLARATION)
    api = graphql("conf_33")
    (implementation,) = (await api(ACTION, id=init["agent"]))["agent"]["implementations"]
    schedule = {"name": "nightly", "action": implementation["action"]["id"], "agent": init["agent"], "interface": "echo", "args": {"x": 4}}

    created = (await api(CREATE, input={**schedule, "cron": "0 2 * * *", "timezone": "Europe/Berlin"}))["createSchedule"]
    # The server said so in the transaction that wrote the row; takt hears it when that commits,
    # well before its reaper's next tick.
    waiting = await waiting_run(api, created["id"])
    read = (await api(READ, id=created["id"]))["schedule"]
    assert waiting and waiting["latestEventKind"] == "QUEUED" and waiting["notBefore"], "takt planned the next 02:00 in Berlin at once"
    assert len(read["upcoming"]) == 2 and read["upcoming"][0] == waiting["notBefore"]
    # A list asks takt for every schedule's slots in one request.
    listed = {row["id"]: row["upcoming"] for row in (await api(LIST))["schedules"]}
    assert listed[created["id"]] == read["upcoming"]

    # Run now: the waiting run itself is moved to now, and takt's reaper dispatches it.
    assert (await api(RUN_NOW, id=created["id"]))["triggerSchedule"]["id"] == waiting["id"]
    assign = await agent.receive_type("ASSIGN")
    assert (assign["task"], assign["args"]) == (waiting["id"], {"x": 4})

    # takt is the one reader of cron lines: its refusal is the mutation's error.
    refused = await api.client.post(api.url, json={"query": CREATE, "variables": {"input": {**schedule, "cron": "whenever"}}}, headers={"Authorization": f"Bearer {api.token}"})
    (error,) = refused.json()["errors"]
    assert 'Not a valid cron line: "whenever"' in error["message"]

    assert (await api(DELETE, id=created["id"]))["deleteSchedule"] == created["id"]

    # Deleting says, in the same transaction, which waiting run goes with the schedule.
    doomed = (await api(CREATE, input={**schedule, "intervalSeconds": 3600}))["createSchedule"]["id"]
    run = await waiting_run(api, doomed)
    assert (await api(DELETE, id=doomed))["deleteSchedule"] == doomed
    for _ in range(40):
        task = (await api("query ($id: ID!) { task(id: $id) { isDone latestEventKind } }", id=run["id"]))["task"]
        if task["isDone"]:
            break
        await asyncio.sleep(0.05)
    assert task["latestEventKind"] == "CANCELLED"
