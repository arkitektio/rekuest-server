"""What only the pair can show: a GraphQL mutation on the server, its effect on agentd's sockets and rows."""

import uuid

from conftest import ECHO_DECLARATION, session_id

DELETE = "mutation ($id: ID!) { deleteAgent(input: {id: $id}) }"
RENAME = "mutation ($id: ID!, $name: String!) { updateAgent(input: {id: $id, name: $name}) { name declaredName } }"
NAMES = "query ($id: ID!) { agent(id: $id) { name declaredName } }"


async def test_a_deleted_agent_is_kicked_and_registers_anew(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_31", session_id=session_id(), hash=f"doomed-{uuid.uuid4().hex}", **ECHO_DECLARATION)
    api = graphql("conf_31")

    assert (await api(DELETE, id=init["agent"]))["deleteAgent"] == init["agent"]

    kick = await agent.receive_type("KICK")
    assert kick["reason"] == "The agent was deleted"
    await agent.close()
    # The process that still runs comes back as a new agent, with its declaration.
    again = await agents()
    reborn = await again.register("conf_31", session_id=session_id(), hash=f"reborn-{uuid.uuid4().hex}", **ECHO_DECLARATION)
    assert reborn["agent"] != init["agent"]
    task = await api.assign(reborn["agent"], "echo", {"x": 1})
    assert (await again.receive_type("ASSIGN"))["task"] == task


async def test_a_rename_survives_a_new_declaration(agents, graphql) -> None:  # noqa: ANN001
    agent = await agents()
    init = await agent.register("conf_32", session_id=session_id(), hash=f"named-{uuid.uuid4().hex}", **ECHO_DECLARATION)
    api = graphql("conf_32")

    renamed = await api(RENAME, id=init["agent"], name="Bench 2")
    assert renamed["updateAgent"] == {"name": "Bench 2", "declaredName": "conformance"}
    await agent.close()

    # A changed declaration, under a new hash and a new declared name: reconciled in full.
    again = await agents()
    await again.register("conf_32", session_id=session_id(), hash=f"renamed-{uuid.uuid4().hex}", **{**ECHO_DECLARATION, "name": "conformance-2"})

    assert (await api(NAMES, id=init["agent"]))["agent"] == {"name": "Bench 2", "declaredName": "conformance-2"}
