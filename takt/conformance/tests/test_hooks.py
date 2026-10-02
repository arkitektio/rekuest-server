"""HookAgents: an agent of kind WEBHOOK speaks the protocol over HTTP.

The server POSTs its frames (an Assign, the mirrors of work the hook assigned) to the hook's
``hook_url``, signed with the shared secret; the hook POSTs its frames to ``/agi/http/<agent>``,
signed the same way, and gets the reply as the response. A small HTTP receiver in this process
plays the hook. The servers must reach it: ``CONFORMANCE_HOOK_HOST`` is the address they POST
to (default 127.0.0.1; for a server in the dokker stack, the stack network's gateway).
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

import httpx
import pytest_asyncio

from conftest import ECHO_DECLARATION, RECEIVE_TIMEOUT, session_id

SECRET = "conformance-hook-secret"


def sign_v1(agent: str, body: bytes, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    payload = f"v1:{agent}:{timestamp}:".encode() + body
    return f"t={timestamp},v1={hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()}"


def verify_v1(agent: str, body: bytes, header: str) -> bool:
    parts = dict(piece.split("=", 1) for piece in header.split(","))
    payload = f"v1:{agent}:{parts['t']}:".encode() + body
    return hmac.compare_digest(hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest(), parts["v1"])


class Receiver:
    """The hook's endpoint: every POST it received, as (headers, body)."""

    def __init__(self) -> None:
        self.received: asyncio.Queue[tuple[dict[str, str], bytes]] = asyncio.Queue()
        self.server: asyncio.Server | None = None
        self.url = ""

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://{os.environ.get('CONFORMANCE_HOOK_HOST', '127.0.0.1')}:{port}/hook"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")[1:]
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines if line)}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            await self.received.put((headers, body))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()

    async def frame(self, kind: str, timeout: float = RECEIVE_TIMEOUT) -> tuple[dict[str, str], dict[str, Any], bytes]:
        """The next POST whose frame is of ``kind`` (earlier ones are dropped)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            headers, body = await asyncio.wait_for(self.received.get(), max(0.01, deadline - asyncio.get_running_loop().time()))
            frame = json.loads(body)
            if frame["type"] == kind:
                return headers, frame, body

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()


@pytest_asyncio.fixture
async def receiver():
    hook = Receiver()
    await hook.start()
    yield hook
    await hook.stop()


def camel(value: Any) -> Any:
    """A declaration as GraphQL input: snake_case keys become camelCase."""
    if isinstance(value, dict):
        return {k.split("_")[0] + "".join(p.title() for p in k.split("_")[1:]): camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [camel(v) for v in value]
    return value


async def hook_agent(graphql, token: str, url: str) -> str:  # noqa: ANN001
    """A HookAgent for ``token`` posting to ``url``, implementing echo: its id."""
    api = graphql(token)
    data = await api(
        "mutation ($input: AgentInput!) { ensureAgent(input: $input) { id } }",
        input={"kind": "WEBHOOK", "hookUrl": url, "hookUrlSecret": SECRET},
    )
    declaration = {k: v for k, v in ECHO_DECLARATION.items() if k in ("name", "implementations")}
    await api(
        "mutation ($input: ImplementAgentInput!) { implementAgent(input: $input) { id } }",
        input=camel({**declaration, "hash": f"hook-{uuid.uuid4().hex}"}),
    )
    return data["ensureAgent"]["id"]


class Hook:
    """The hook's side of the intake: signed POSTs to ``/agi/http/<agent>``."""

    def __init__(self, http: httpx.AsyncClient, target, agent: str) -> None:  # noqa: ANN001
        self.http, self.url, self.agent = http, f"{target.http}/agi/http/{agent}", agent

    async def post(self, frame: dict[str, Any], *, signed: bool = True, raw: tuple[bytes, str] | None = None) -> httpx.Response:
        body, signature = raw if raw is not None else (json.dumps({"id": str(uuid.uuid4()), **frame}).encode(), None)
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-Rekuest-Signature-V1"] = signature or sign_v1(self.agent, body)
        return await self.http.post(self.url, content=body, headers=headers)

    def signed(self, frame: dict[str, Any]) -> tuple[bytes, str]:
        body = json.dumps({"id": str(uuid.uuid4()), **frame}).encode()
        return body, sign_v1(self.agent, body)


async def test_the_intake_refuses_what_is_not_the_hooks(graphql, http, target, receiver) -> None:  # noqa: ANN001
    agent = await hook_agent(graphql, "conf_27", receiver.url)
    hook = Hook(http, target, agent)
    frame = {"type": "LOG", "task": "1", "message": "hi", "level": "INFO"}

    assert (await hook.post(frame, signed=False)).status_code == 401
    body = json.dumps({"id": str(uuid.uuid4()), **frame}).encode()
    assert (await hook.post(frame, raw=(body, sign_v1(agent, body, int(time.time()) - 3600)))).status_code == 401, "outside the skew"
    assert (await hook.post(frame, raw=(body, sign_v1(str(int(agent) + 100000), body)))).status_code in (401, 404)
    assert (await http.post(f"{target.http}/agi/http/999999999", content=body, headers={"X-Rekuest-Signature-V1": sign_v1("999999999", body)})).status_code == 404


async def test_a_hook_is_assigned_to_and_reports_over_http(agents, graphql, http, target, receiver) -> None:  # noqa: ANN001
    agent = await hook_agent(graphql, "conf_28", receiver.url)
    caller = await agents()
    caller_init = await caller.register("conf_29", session_id=session_id(), **ECHO_DECLARATION)
    parent = await graphql("conf_29").assign(caller_init["agent"], "echo", {"x": 1})
    await caller.receive_type("ASSIGN")

    await caller.send({"id": str(uuid.uuid4()), "type": "ASSIGN_REQUEST", "reference": str(uuid.uuid4()), "parent": parent, "agent": agent, "interface": "echo", "args": {"x": 2}})
    child = (await caller.receive_type("ASSIGN_RESPONSE"))["task"]

    headers, assign, body = await receiver.frame("ASSIGN")
    assert assign["task"] == child and assign["args"] == {"x": 2}
    assert headers["x-rekuest-agent"] == agent
    assert verify_v1(agent, body, headers["x-rekuest-signature-v1"]), "the delivery is signed for this hook"

    hook = Hook(http, target, agent)
    started = await hook.post({"type": "STARTED", "task": child, "seq": 1})
    assert started.status_code == 200 and started.json()["type"] == "EVENT_ACK", started.text
    completed = hook.signed({"type": "COMPLETED", "task": child, "seq": 2})
    first = await hook.post({}, raw=completed)
    assert first.status_code == 200 and first.json()["type"] == "EVENT_ACK", first.text
    # The same request again (an HTTP retry, or a replay): acked, not recorded twice.
    again = await hook.post({}, raw=completed)
    assert again.status_code == 200 and again.json()["type"] == "EVENT_ACK", again.text

    kinds = []
    while "COMPLETED_EVENT" not in kinds:
        frame = await caller.receive(RECEIVE_TIMEOUT)
        if frame["type"] == "HEARTBEAT":
            await caller.send({"type": "HEARTBEAT_ANSWER"})
        elif frame.get("task") == child:
            kinds.append(frame["type"])
    assert kinds.count("COMPLETED_EVENT") == 1, kinds


async def test_a_hook_assigns_over_http_and_gets_the_mirrors_posted(agents, graphql, http, target, receiver) -> None:  # noqa: ANN001
    agent = await hook_agent(graphql, "conf_30", receiver.url)
    parent = await graphql("conf_30").assign(agent, "echo", {"x": 1})
    _, assign, _ = await receiver.frame("ASSIGN")
    assert assign["task"] == parent

    executor = await agents()
    executor_init = await executor.register("conf_29", session_id=session_id(), force=True, **ECHO_DECLARATION)
    hook = Hook(http, target, agent)
    response = await hook.post({"type": "ASSIGN_REQUEST", "reference": str(uuid.uuid4()), "parent": parent, "agent": executor_init["agent"], "interface": "echo", "args": {"x": 5}})
    assert response.status_code == 200 and response.json()["type"] == "ASSIGN_RESPONSE", response.text
    child = response.json()["task"]

    assert (await executor.receive_type("ASSIGN"))["task"] == child
    await executor.send({"type": "STARTED", "task": child, "seq": 1})
    await executor.send({"type": "COMPLETED", "task": child, "seq": 2})

    headers, mirror, body = await receiver.frame("COMPLETED_EVENT")
    assert mirror["task"] == child
    assert verify_v1(agent, body, headers["x-rekuest-signature-v1"])
