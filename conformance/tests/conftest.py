"""The conformance stack and a raw agent.

Tests speak the wire protocol directly, with no SDK in between: a frame is a dict, a close
is a code. The target is either an already running server (``CONFORMANCE_URL``, e.g.
``http://localhost:5690`` for the Python server or agentd) or the stack in ``stack/``,
brought up with dokker for the session.
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
    """Where the protocol is served: its websocket and HTTP bases."""

    def __init__(self, http: str) -> None:
        self.http = http.rstrip("/")
        self.ws = self.http.replace("http://", "ws://").replace("https://", "wss://")

    @property
    def agi(self) -> str:
        return f"{self.ws}/agi"


@pytest_asyncio.fixture(scope="session")
async def target() -> AsyncIterator[Target]:
    url = os.environ.get("CONFORMANCE_URL")
    if url:
        yield Target(url)
        return

    from dokker import testing

    # A gitignored ``stack/docker-compose.local.yml`` layers local changes on the stack, e.g. the
    # server's source mounted over the image's to judge unreleased code.
    local = STACK.with_name("docker-compose.local.yml")
    setup = testing([str(STACK), *([str(local)] if local.exists() else [])])
    setup.add_health_check(
        url=lambda spec: f"http://localhost:{spec.find_service('rekuest').get_port_for_internal(80).published}/graphql",
        service="rekuest",
        timeout=5,
        max_retries=30,
    )
    async with setup:
        await setup.adown()
        await setup.apull()
        await setup.aup()
        await setup.acheck_health()
        port = setup.spec.find_service("rekuest").get_port_for_internal(80).published
        yield Target(f"http://localhost:{port}")


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
