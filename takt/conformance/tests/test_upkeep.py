"""The pair with no third process: the agent endpoint's name, and one embedding model in two languages."""

import copy
import uuid

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


async def test_an_action_takt_registered_is_found_by_the_servers_search(agents, graphql) -> None:  # noqa: ANN001
    """takt embeds the action it writes, in Rust; the server embeds a search, in Python.

    They are one model only if the two vectors land in the same space, and this is where
    that is held: a query sharing no word with the action's name can find it by nothing
    but the distance between a vector takt wrote and one the server computed. The vector
    is there as soon as the registration is acknowledged: nothing comes by later to fill it.
    """
    marker = uuid.uuid4().hex[:8]
    name = f"Count the nuclei {marker}"
    declared = copy.deepcopy(ECHO_DECLARATION)
    declared["implementations"][0]["definition"].update(key=f"count_{uuid.uuid4().hex}", name=name, description="Count the nuclei in a fluorescence image")
    agent = await agents()
    await agent.register("conf_36", session_id=session_id(), **declared)
    ask = graphql("conf_36")

    registered = (await ask("query ($name: String!) { actions(filters: {name: {exact: $name}}) { id embedding } }", name=name))["actions"]
    assert len(registered) == 1, "the action is registered"
    embedding = registered[0]["embedding"]
    assert embedding is not None, "takt wrote the action without a vector"
    model, _, numbers = embedding.partition(":")
    assert model and len(numbers.split(",")) == 256

    # The same text, embedded by the server: the nearest thing to it is the row takt embedded.
    found = (await ask("query ($search: String!) { actions(filters: {search: $search}) { id } }", search=f"{name}\nCount the nuclei in a fluorescence image"))["actions"]
    assert registered[0]["id"] in [action["id"] for action in found]
    # And a paraphrase with none of the name's words: only the semantic leg can find it.
    paraphrased = (await ask("query ($search: String!) { actions(filters: {search: $search}) { id } }", search="counting cell nuclei in microscopy images"))["actions"]
    assert registered[0]["id"] in [action["id"] for action in paraphrased]
