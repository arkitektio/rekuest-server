"""The conformance stack and a raw agent.

Tests speak the wire protocol directly, with no SDK in between: a frame is a dict, a close
is a code. The target is the pair in ``stack/`` (the rekuest server and takt, both built
from this repository and brought up with dokker for the session), or an already running pair:
``CONFORMANCE_URL`` is takt, ``CONFORMANCE_GRAPHQL_URL`` the server's GraphQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import websockets
from websockets.asyncio.client import ClientConnection

STACK = Path(__file__).resolve().parents[1] / "stack" / "docker-compose.yml"
RECEIVE_TIMEOUT = 10.0


class Target:
    """Where the protocol is served (takt: websocket and HTTP) and the server's GraphQL API."""

    def __init__(self, http: str, graphql: str | None = None) -> None:
        self.http = http.rstrip("/")
        self.ws = self.http.replace("http://", "ws://").replace("https://", "wss://")
        self.graphql = (graphql or f"{self.http}/graphql").rstrip("/")

    @property
    def agi(self) -> str:
        """The agent socket under its former name: what every released agent asks for."""
        return f"{self.ws}/agi"

    @property
    def agent(self) -> str:
        return f"{self.ws}/agent"


@pytest_asyncio.fixture(scope="session")
async def target() -> AsyncIterator[Target]:
    url = os.environ.get("CONFORMANCE_URL")
    if url:
        yield Target(url, os.environ.get("CONFORMANCE_GRAPHQL_URL"))
        return

    from dokker import testing

    # A gitignored ``stack/docker-compose.local.yml`` layers local changes on the stack.
    local = STACK.with_name("docker-compose.local.yml")
    # The teardown's wall clock is there to catch a hang, not to fail a green run on a slow runner.
    setup = testing([str(STACK), *([str(local)] if local.exists() else [])], teardown_timeout=60)

    def published(service: str, internal: int):  # noqa: ANN202
        return lambda spec: spec.find_service(service).get_port_for_internal(internal).published

    setup.add_health_check(url=lambda spec: f"http://localhost:{published('rekuest', 80)(spec)}/graphql", service="rekuest", timeout=5, max_retries=60)
    setup.add_health_check(url=lambda spec: f"http://localhost:{published('takt', 8080)(spec)}/ht", service="takt", timeout=5, max_retries=60)
    # takt, in the stack, reaches the hook scenarios' receiver on the host.
    os.environ.setdefault("CONFORMANCE_HOOK_HOST", "host.docker.internal")
    async with setup:
        await setup.adown()
        await setup.apull(services=["redis", "db", "rustfs", "initc"])
        await setup.aup(build=True)
        await setup.acheck_health()
        yield Target(
            f"http://localhost:{published('takt', 8080)(setup.spec)}",
            f"http://localhost:{published('rekuest', 80)(setup.spec)}/graphql",
        )


class Closed(Exception):
    """The server closed the socket; ``code`` is why."""

    def __init__(self, code: int | None, reason: str) -> None:
        super().__init__(f"closed with {code}: {reason}")
        self.code = code
        self.reason = reason


class RawAgent:
    """One agent socket, spoken to in frames."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def send(self, frame: dict[str, Any] | str) -> None:
        await self.ws.send(frame if isinstance(frame, str) else json.dumps({"id": str(uuid.uuid4()), **frame}))

    async def receive(self, timeout: float = RECEIVE_TIMEOUT) -> dict[str, Any]:
        try:
            return json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        except websockets.ConnectionClosed as e:
            raise Closed(e.rcvd.code if e.rcvd else None, e.rcvd.reason if e.rcvd else "") from None

    async def receive_type(self, kind: str, timeout: float = RECEIVE_TIMEOUT) -> dict[str, Any]:
        """The next frame of ``kind``, skipping heartbeats (answered) and anything else."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            frame = await self.receive(max(0.01, deadline - loop.time()))
            if frame["type"] == kind:
                return frame
            if frame["type"] == "HEARTBEAT":
                await self.send({"type": "HEARTBEAT_ANSWER"})

    async def expect_close(self, timeout: float = RECEIVE_TIMEOUT) -> int | None:
        """Read until the server closes; the close code."""
        try:
            while True:
                await self.receive(timeout)
        except Closed as closed:
            return closed.code

    async def register(self, token: str, *, session_id: str | None = None, force: bool = False, **declaration: Any) -> dict[str, Any]:
        await self.send({"type": "REGISTER", "token": token, "force": force, "session_id": session_id, **declaration})
        return await self.receive_type("INIT")

    async def close(self) -> None:
        await self.ws.close()


@pytest_asyncio.fixture
async def agents(target: Target) -> AsyncIterator[Any]:
    """``await agents()`` opens a raw agent socket; every one is closed when the test ends."""
    opened: list[RawAgent] = []

    async def open_socket() -> RawAgent:
        agent = RawAgent(await websockets.connect(target.agi, open_timeout=10))
        opened.append(agent)
        return agent

    yield open_socket
    await asyncio.gather(*(agent.close() for agent in opened), return_exceptions=True)


@pytest_asyncio.fixture
async def http(target: Target) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=target.http, timeout=10) as client:
        yield client


def session_id() -> str:
    return f"conformance-{uuid.uuid4().hex[:12]}"


# What an agent with one action (``echo(x: int) -> int``) declares, as the Python client builds it.
ECHO_DECLARATION: dict[str, Any] = {
    "name": "conformance",
    "states": [],
    "locks": [],
    "bloks": [],
    "implementations": [
        {
            "definition": {
                "description": "Echo", "collections": [], "key": "echo", "version": "1", "name": "Echo",
                "stateful": False, "pure": False, "idempotent": False, "allow_probe": False, "catalogs": [],
                "port_groups": [],
                "args": [{"key": "x", "kind": "INT", "nullable": False, "effects": [], "validators": []}],
                "returns": [{"key": "return0", "kind": "INT", "nullable": False, "effects": []}],
                "kind": "FUNCTION", "is_test_for": [], "is_dev": False,
            },
            "dependencies": [], "tracks": [], "interface": "echo", "locks": [], "optimistics": [],
            "manipulates": [], "needs_token": True, "effects": "UNKNOWN", "execution": "PLAIN",
        }
    ],
}


class GraphQL:
    """The Python server's GraphQL API, as one static token."""

    def __init__(self, client: httpx.AsyncClient, url: str, token: str) -> None:
        self.client, self.url, self.token = client, url, token

    async def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        response = await self.client.post(self.url, json={"query": query, "variables": variables}, headers={"Authorization": f"Bearer {self.token}"})
        body = response.json()
        assert not body.get("errors"), body["errors"]
        return body["data"]

    async def assign(self, agent: str, interface: str, args: dict[str, Any]) -> str:
        data = await self(
            "mutation ($input: AssignInput!) { assign(input: $input) { id } }",
            input={"agent": agent, "interface": interface, "args": args, "capture": False, "reference": str(uuid.uuid4())},
        )
        return data["assign"]["id"]

    async def task(self, task: str) -> dict[str, Any]:
        data = await self("query ($id: ID!) { task(id: $id) { latestEventKind events { kind agentPos } } }", id=task)
        return data["task"]


@pytest_asyncio.fixture
async def graphql(target: Target) -> AsyncIterator[Any]:
    """``graphql(token)``: the GraphQL API as that static token."""
    async with httpx.AsyncClient(timeout=10) as client:
        yield lambda token: GraphQL(client, target.graphql, token)
