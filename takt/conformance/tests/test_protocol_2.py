"""Agent protocol 2: both directions numbered and acknowledged, a gap refused, a task asked about.

The scenarios here are what the protocol promises beyond "frames are understood": a command is
kept until the agent says it arrived, the handshake says where both sides stand, a gap in the
agent's journal is never passed over silently, and an agent that no longer knows a task has lost it.
"""

import asyncio
import uuid

from conftest import ECHO_DECLARATION, session_id

REGISTRATION_REJECTED = 4006


async def test_an_agent_that_does_not_speak_protocol_2_is_refused(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send({"type": "REGISTER", "token": "conf_68", "session_id": session_id(), "spec_version": "9.1.0"})

    error = await agent.receive()
    assert error["type"] == "PROTOCOL_ERROR" and "protocol_2" in error["error"], error
    assert await agent.expect_close() == REGISTRATION_REJECTED


async def test_a_command_is_kept_until_acknowledged_and_sent_again_after_a_reconnect(agents, graphql) -> None:  # noqa: ANN001
    session = session_id()
    first = await agents()
    first.auto_ack = False
    init = await first.register("conf_69", session_id=session, **ECHO_DECLARATION)
    task = await graphql("conf_69").assign(init["agent"], "echo", {"x": 1})

    assign = await first.receive_where(lambda frame: frame["type"] == "ASSIGN" and frame["task"] == task)
    assert isinstance(assign["pos"], int) and assign["pos"] >= 1, "a command carries its position"
    await first.close()

    # Never acknowledged: the same process, back, is sent it again at the same position.
    second = await agents()
    second.auto_ack = False
    await second.register("conf_69", session_id=session, force=True, **ECHO_DECLARATION)
    again = await second.receive_where(lambda frame: frame["type"] == "ASSIGN" and frame["task"] == task)
    assert again["pos"] == assign["pos"]
    await second.send({"type": "COMMAND_ACK", "through": again["pos"]})
    await asyncio.sleep(0.3)
    await second.close()

    # Acknowledged, and saying so in the handshake: it does not come a third time.
    third = await agents()
    await third.register("conf_69", session_id=session, force=True, commands_seen=again["pos"], **ECHO_DECLARATION)
    try:
        stray = await third.receive_where(lambda frame: frame["type"] == "ASSIGN" and frame["task"] == task, timeout=1.5)
    except TimeoutError:
        stray = None
    assert stray is None, "an acknowledged command is forgotten"


async def test_a_gap_in_the_journal_is_refused_until_the_agent_fills_it_or_gives_it_up(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_70", session_id=session_id(), **ECHO_DECLARATION)
    api = graphql("conf_70")
    task = await api.assign(init["agent"], "echo", {"x": 1})
    await agent.receive_where(lambda frame: frame["type"] == "ASSIGN" and frame["task"] == task)
    journal = f"journal-{uuid.uuid4().hex[:8]}"

    def numbered(pos: int, frame: dict) -> dict:  # noqa: ANN001
        return {**frame, "pos": pos, "journal_session": journal, "agent_ts": 1790715513.5}

    await agent.send(numbered(1, {"type": "STARTED", "task": task, "task_step": 1}))
    # Position 2 never arrives; 3 does, and is not taken.
    await agent.send(numbered(3, {"type": "COMPLETED", "task": task, "task_step": 3}))
    nack = await agent.receive_type("JOURNAL_NACK")
    assert (nack["journal_session"], nack["expected"]) == (journal, 2)
    assert (await api.task(task))["latestEventKind"] != "COMPLETED", "what came ahead of its turn was not taken"

    # The agent no longer holds 2 and says so; 3, sent again, is then the next position.
    await agent.send({"type": "JOURNAL_SKIP", "session": journal, "first": 2, "last": 2})
    await agent.send(numbered(3, {"type": "COMPLETED", "task": task, "task_step": 3}))
    ack = await agent.receive_where(lambda frame: frame["type"] == "JOURNAL_ACK" and frame["through"] >= 3)
    assert ack["journal_session"] == journal
    assert (await api.task(task))["latestEventKind"] == "COMPLETED"

    # And the handshake says how far the server has that session.
    await agent.close()
    back = await agents()
    init = await back.register("conf_70", session_id=session_id(), force=True, sessions=[journal], **ECHO_DECLARATION)
    assert init["positions"] == {journal: 3}


async def test_an_agent_asked_about_a_task_it_does_not_know_has_lost_it(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_71", session_id=session_id(), **ECHO_DECLARATION)
    api = graphql("conf_71")
    task = await api.assign(init["agent"], "echo", {"x": 1})
    await agent.receive_where(lambda frame: frame["type"] == "ASSIGN" and frame["task"] == task)
    await asyncio.sleep(0.3)  # the acknowledgement is the pickup: only then is the agent asked

    async def ask() -> dict:
        data = await api("mutation ($input: InquireInput!) { inquire(input: $input) { status asked detail progress message } }", input={"task": task})
        return data["inquire"]

    async def answer(status: str, **said: object) -> None:
        question = await agent.receive_type("INQUIRE")
        assert question["task"] == task
        await agent.send({"type": "INQUIRY_ANSWER", "task": task, "request": question["request"], "status": status, **said})

    asked, _ = await asyncio.gather(ask(), answer("RUNNING", progress=40, message="half way"))
    assert (asked["status"], asked["asked"], asked["progress"], asked["message"]) == ("RUNNING", True, 40, "half way")
    assert (await api.task(task))["latestEventKind"] != "LOST"

    asked, _ = await asyncio.gather(ask(), answer("UNKNOWN"))
    assert asked["status"] == "UNKNOWN"
    assert (await api.task(task))["latestEventKind"] == "LOST", "the one agent that could have it does not"
