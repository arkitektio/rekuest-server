"""One live connection per agent: who is refused, who takes over."""

from conftest import session_id

ALREADY_CONNECTED = 4004
REPLACED = 4005


async def test_another_process_is_refused_while_the_first_is_live(agents) -> None:  # noqa: ANN001
    incumbent = await agents()
    await incumbent.register("conf_3", session_id=session_id())

    intruder = await agents()
    await intruder.send({"type": "REGISTER", "token": "conf_3", "session_id": session_id()})

    error = await intruder.receive()
    assert error["type"] == "PROTOCOL_ERROR"
    assert await intruder.expect_close() == ALREADY_CONNECTED


async def test_force_takes_over_and_the_incumbent_is_replaced(agents) -> None:  # noqa: ANN001
    incumbent = await agents()
    agent_id = (await incumbent.register("conf_4", session_id=session_id()))["agent"]

    taker = await agents()
    assert (await taker.register("conf_4", session_id=session_id(), force=True))["agent"] == agent_id

    assert await incumbent.expect_close() == REPLACED


async def test_the_same_process_reconnecting_takes_over_without_force(agents) -> None:  # noqa: ANN001
    # The process noticed a drop before the server did: its old connection still looks live.
    session = session_id()
    incumbent = await agents()
    agent_id = (await incumbent.register("conf_5", session_id=session))["agent"]

    again = await agents()
    assert (await again.register("conf_5", session_id=session))["agent"] == agent_id

    assert await incumbent.expect_close() == REPLACED


async def test_a_closed_connection_frees_the_agent(agents) -> None:  # noqa: ANN001
    first = await agents()
    await first.register("conf_6", session_id=session_id())
    await first.close()

    second = await agents()
    assert (await second.register("conf_6", session_id=session_id()))["type"] == "INIT"
