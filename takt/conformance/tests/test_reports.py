"""Reports: what an agent says about its work is persisted once and acknowledged.

The agent declares one action, is assigned a task through GraphQL, and reports on it: plain
frames are answered with EVENT_ACK (lifecycle and terminals), numbered frames with one
cumulative JOURNAL_ACK and never an EVENT_ACK.
"""

import uuid

from conftest import ECHO_DECLARATION, session_id

TERMINAL_ACK_TIMEOUT = 5.0


async def assigned(agents, graphql, token):  # noqa: ANN001, ANN201
    """A registered echo agent and a task assigned to it: (agent socket, api, task id)."""
    agent = await agents()
    init = await agent.register(token, session_id=session_id(), **ECHO_DECLARATION)
    api = graphql(token)
    task = await api.assign(init["agent"], "echo", {"x": 3})
    assign = await agent.receive_type("ASSIGN")
    assert assign["task"] == task and assign["args"] == {"x": 3}
    return agent, api, task


async def test_plain_reports_are_acked_and_persisted(agents, graphql) -> None:  # noqa: ANN001
    agent, api, task = await assigned(agents, graphql, "conf_9")

    await agent.send({"type": "STARTED", "task": task, "seq": 1})
    started = await agent.receive_type("EVENT_ACK")
    assert started["task"] == task and started["seq"] == 1

    await agent.send({"type": "PROGRESS", "task": task, "progress": 50, "seq": 2})
    await agent.send({"type": "YIELD", "task": task, "returns": {"return0": 3}, "seq": 3})
    completed_id = str(uuid.uuid4())
    await agent.send({"id": completed_id, "type": "COMPLETED", "task": task, "seq": 4})
    completed = await agent.receive_type("EVENT_ACK", timeout=TERMINAL_ACK_TIMEOUT)
    assert completed["event"] == completed_id and completed["seq"] == 4

    # The agent retries a terminal it thinks was lost: acked again, recorded once.
    await agent.send({"id": completed_id, "type": "COMPLETED", "task": task, "seq": 4})
    await agent.receive_type("EVENT_ACK", timeout=TERMINAL_ACK_TIMEOUT)

    history = await api.task(task)
    assert history["latestEventKind"] == "COMPLETED"
    kinds = [event["kind"] for event in history["events"]]
    assert kinds.count("COMPLETED") == 1
    assert [k for k in kinds if k in ("STARTED", "PROGRESS", "YIELD", "COMPLETED")] == ["STARTED", "PROGRESS", "YIELD", "COMPLETED"]


async def test_numbered_reports_are_journal_acked_once(agents, graphql) -> None:  # noqa: ANN001
    agent, api, task = await assigned(agents, graphql, "conf_10")
    journal = f"journal-{uuid.uuid4().hex[:8]}"

    def numbered(pos: int, frame: dict) -> dict:
        return {**frame, "pos": pos, "journal_session": journal, "task_step": pos, "agent_ts": 1790715513.5}

    await agent.send(numbered(1, {"type": "STARTED", "task": task}))
    await agent.send(numbered(2, {"type": "LOG", "task": task, "message": "working", "level": "INFO"}))
    await agent.send(numbered(3, {"type": "YIELD", "task": task, "returns": {"return0": 3}}))
    await agent.send(numbered(4, {"type": "COMPLETED", "task": task}))

    # Frames up to the terminal are acknowledged together, at once after it; never EVENT_ACK.
    seen = []
    while True:
        frame = await agent.receive(timeout=TERMINAL_ACK_TIMEOUT)
        if frame["type"] == "HEARTBEAT":
            await agent.send({"type": "HEARTBEAT_ANSWER"})
            continue
        seen.append(frame["type"])
        if frame["type"] == "JOURNAL_ACK" and frame["pos"] == 4:
            assert frame["journal_session"] == journal
            break
    assert "EVENT_ACK" not in seen

    # A resend after a reconnect (the agent held it unacked): skipped, acknowledged again.
    await agent.send(numbered(4, {"type": "COMPLETED", "task": task}))
    ack = await agent.receive_type("JOURNAL_ACK", timeout=TERMINAL_ACK_TIMEOUT)
    assert ack["pos"] == 4

    history = await api.task(task)
    assert history["latestEventKind"] == "COMPLETED"
    positions = [(e["kind"], e["agentPos"]) for e in history["events"] if e["agentPos"] is not None]
    assert positions == [("STARTED", 1), ("LOG", 2), ("YIELD", 3), ("COMPLETED", 4)]


async def test_a_report_on_a_task_that_is_not_the_agents_is_dropped(agents, graphql) -> None:  # noqa: ANN001
    _, api, task = await assigned(agents, graphql, "conf_11")

    stranger = await agents()
    await stranger.register("conf_12", session_id=session_id())
    await stranger.send({"type": "COMPLETED", "task": task, "seq": 1})
    await stranger.receive_type("EVENT_ACK")  # acked (the agent must stop retaining it), not applied

    assert (await api.task(task))["latestEventKind"] != "COMPLETED"
