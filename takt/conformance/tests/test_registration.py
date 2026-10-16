"""Registering: the first frame, what a server answers, and what it refuses."""

from conftest import session_id

NOT_JSON = 3002
SCHEMA = 3003


async def test_a_register_is_answered_with_init(agents) -> None:  # noqa: ANN001
    agent = await agents()

    init = await agent.register("conf_1", session_id=session_id())

    assert init["type"] == "INIT"
    assert init["agent"], "INIT names the agent"
    assert isinstance(init.get("inquiries", []), list)


async def test_the_same_token_is_the_same_agent(agents) -> None:  # noqa: ANN001
    first = await agents()
    agent_id = (await first.register("conf_2", session_id=session_id()))["agent"]
    await first.close()

    again = await agents()
    assert (await again.register("conf_2", session_id=session_id()))["agent"] == agent_id


async def test_a_frame_that_is_not_json_closes(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send("this is not json")

    assert await agent.expect_close() == NOT_JSON


async def test_a_frame_the_schema_refuses_is_answered_then_closed(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send({"type": "REGISTER"})  # no token

    error = await agent.receive()
    assert error["type"] == "PROTOCOL_ERROR" and error["error"]
    assert await agent.expect_close() == SCHEMA


async def test_the_first_frame_must_be_register(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send({"type": "HEARTBEAT_ANSWER"})

    assert await agent.expect_close() == SCHEMA


async def test_an_unknown_token_is_refused(agents) -> None:  # noqa: ANN001
    agent = await agents()

    await agent.send({"type": "REGISTER", "token": "not-a-token", "session_id": session_id()})

    assert await agent.expect_close() == SCHEMA


async def test_every_register_states_the_supervisor(agents, graphql) -> None:  # noqa: ANN001
    """The supervisor rides every REGISTER, outside the declaration: a reconnect that declares
    nothing new still replaces it, and one that names none clears it."""
    api = graphql("conf_66")
    query = "query ($id: ID!) { agent(id: $id) { supervisor } }"

    first = await agents()
    agent_id = (await first.register("conf_66", session_id=session_id(), supervisor="plugin_engine"))["agent"]
    await first.close()
    assert (await api(query, id=agent_id))["agent"]["supervisor"] == "plugin_engine"

    again = await agents()
    await again.register("conf_66", session_id=session_id(), supervisor="local_launcher")
    await again.close()
    assert (await api(query, id=agent_id))["agent"]["supervisor"] == "local_launcher"

    unsupervised = await agents()
    await unsupervised.register("conf_66", session_id=session_id())
    await unsupervised.close()
    assert (await api(query, id=agent_id))["agent"]["supervisor"] is None
