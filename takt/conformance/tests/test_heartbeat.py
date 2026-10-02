"""Liveness: the server pings, an agent that answers stays, one that does not is closed.

The Python server pings every 10 s and waits 5 s for the answer, so these take a while.
"""

import pytest

from conftest import session_id

HEARTBEAT_NOT_ANSWERED = 3001

pytestmark = pytest.mark.slow


async def test_an_agent_that_answers_stays_connected(agents) -> None:  # noqa: ANN001
    agent = await agents()
    await agent.register("conf_7", session_id=session_id())

    for _ in range(2):
        heartbeat = await agent.receive_type("HEARTBEAT", timeout=20)
        assert heartbeat["type"] == "HEARTBEAT"
        await agent.send({"type": "HEARTBEAT_ANSWER"})


async def test_an_agent_that_does_not_answer_is_closed(agents) -> None:  # noqa: ANN001
    agent = await agents()
    await agent.register("conf_8", session_id=session_id())

    heartbeat = await agent.receive(timeout=20)
    assert heartbeat["type"] == "HEARTBEAT"

    assert await agent.expect_close(timeout=20) == HEARTBEAT_NOT_ANSWERED
