"""The server's GraphQL feeds hear what takt does: takt publishes, the server's subscriptions deliver."""

import asyncio
import json
import uuid

import websockets
from conftest import ECHO_DECLARATION, session_id

AGENTS = "subscription { agents { create { id connected } update { id connected } delete } }"


async def changes_of(feed, agent: str, connected: bool, timeout: float = 10.0) -> None:  # noqa: ANN001
    """Read the feed until it says ``agent`` is (not) connected."""
    async with asyncio.timeout(timeout):
        while True:
            frame = json.loads(await feed.recv())
            if frame["type"] != "next":
                continue
            event = frame["payload"]["data"]["agents"]
            change = event["update"] or event["create"]
            if change and change["id"] == agent and change["connected"] is connected:
                return


async def test_the_agents_feed_shows_an_agent_come_and_go(agents, target) -> None:  # noqa: ANN001
    held = f"watched-{uuid.uuid4().hex}"
    first = await agents()
    agent = (await first.register("conf_34", session_id=session_id(), hash=held, **ECHO_DECLARATION))["agent"]
    await first.close()

    url = target.graphql.replace("http://", "ws://").replace("https://", "wss://")
    async with websockets.connect(url, subprotocols=["graphql-transport-ws"]) as feed:
        await feed.send(json.dumps({"type": "connection_init", "payload": {"token": "conf_34"}}))
        assert json.loads(await feed.recv())["type"] == "connection_ack"
        await feed.send(json.dumps({"id": "1", "type": "subscribe", "payload": {"query": AGENTS}}))
        await asyncio.sleep(1)  # the subscription joins its group; whatever the first session left has passed

        # The declaration is the one the agent holds: nothing is reconciled, only the lease changes.
        again = await agents()
        await again.register("conf_34", session_id=session_id(), hash=held, **ECHO_DECLARATION)
        await changes_of(feed, agent, connected=True)

        await again.close()
        await changes_of(feed, agent, connected=False)
