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


def _attach(protocols: list[str] | None) -> dict:
    """One `attach(scene: str)` implementation, hinted as `protocols` when given."""
    definition = {"key": "attach", "version": "1", "name": "Attach", "kind": "FUNCTION",
                  "args": [{"key": "scene", "kind": "STRING", "nullable": False}], "returns": []}
    if protocols is not None:
        definition["protocols"] = protocols
    return {"interface": "attach", "definition": definition}


async def test_protocol_hints_register_and_find_the_agent(agents, graphql) -> None:  # noqa: ANN001
    """A definition's protocol hints land with the registration: the agent is found by them
    (`implementsProtocols`), its implementation filters by them, and a reconnect that changes
    only the hints (and so the agent's hash, never the action's) rewrites them."""
    api = graphql("conf_67")
    find = "query ($p: [String!]!) { agents(filters: {implementsProtocols: $p}) { id } }"
    hooks = """query ($id: ID!, $p: [String!]!) {
        implementations(filters: {agent: {ids: [$id]}, action: {protocols: $p}}) { interface action { hash protocols { name } } }
    }"""

    first = await agents()
    agent_id = (await first.register("conf_67", session_id=session_id(), hash="hints-1", implementations=[_attach(["on_scene_init"])]))["agent"]
    await first.close()
    assert agent_id in [a["id"] for a in (await api(find, p=["on_scene_init"]))["agents"]]
    (listed,) = (await api(hooks, id=agent_id, p=["on_scene_init"]))["implementations"]
    assert [p["name"] for p in listed["action"]["protocols"]] == ["on_scene_init"]

    again = await agents()
    await again.register("conf_67", session_id=session_id(), hash="hints-2", implementations=[_attach(["on_scene_leave"])])
    await again.close()
    assert (await api(hooks, id=agent_id, p=["on_scene_init"]))["implementations"] == []
    (moved,) = (await api(hooks, id=agent_id, p=["on_scene_leave"]))["implementations"]
    assert moved["action"]["hash"] == listed["action"]["hash"], "a hint is not the action's identity"

