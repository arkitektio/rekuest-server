"""The pair with no third process: the agent endpoint's name, and the server's upkeep on takt's clock."""

import asyncio
import copy
import uuid

import pytest
import websockets
from conftest import ECHO_DECLARATION, RawAgent, session_id


async def test_the_agent_socket_answers_under_its_name(target) -> None:  # noqa: ANN001
    """``/agent`` is the endpoint; ``/agi`` (what every other test connects to) is its former name."""
    agent = RawAgent(await websockets.connect(target.agent, open_timeout=10))
    try:
        init = await agent.register("conf_35", session_id=session_id())
        assert init["type"] == "INIT" and init["agent"]
    finally:
        await agent.close()


@pytest.mark.slow
async def test_an_action_takt_registered_is_embedded_by_the_server(agents, graphql) -> None:  # noqa: ANN001
    """takt writes the action without a vector; only the server has the model. Nothing but takt
    asking the server for its ``reembed`` job (every 30 s) fills it in: there is no reaper."""
    name = f"Count the nuclei {uuid.uuid4().hex[:8]}"
    declared = copy.deepcopy(ECHO_DECLARATION)
    declared["implementations"][0]["definition"].update(key=f"count_{uuid.uuid4().hex}", name=name, description="Count the nuclei in a fluorescence image")
    agent = await agents()
    await agent.register("conf_36", session_id=session_id(), **declared)

    query = "query ($name: String!) { actions(filters: {name: {exact: $name}}) { id embedding } }"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 75
    while True:
        actions = (await graphql("conf_36")(query, name=name))["actions"]
        assert len(actions) == 1, "the action is registered"
        if actions[0]["embedding"] is not None:
            return
        assert loop.time() < deadline, "the action was never embedded: takt did not ask, or the server did not answer"
        await asyncio.sleep(2)
