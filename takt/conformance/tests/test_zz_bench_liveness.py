"""The liveness benchmark: does an agent stay served while the UI hammers GraphQL?

Run against any target, like the rest of the suite; skipped unless ``BENCH_SECONDS`` is set:

    BENCH_SECONDS=30 BENCH_RATES=0,10,26 CONFORMANCE_URL=... pytest -s tests/test_zz_bench_liveness.py

One agent streams numbered reports (a YIELD every 50 ms, a task of 100 at a time, as the
Python client does) while ``BENCH_CONCURRENCY`` workers each run task-list and task-detail
queries at ``rate``/s against the GraphQL API. Per rate it prints:

- report lag: a report sent → the JOURNAL_ACK that covers it (the server's report pipeline);
- assign: the GraphQL assign mutation → the ASSIGN frame on the agent's socket;
- heartbeat: the spacing of the server's HEARTBEATs (answered at once) and every 3001 kick;
- query: the GraphQL latency the load itself saw.

First run (2026-09-30, Python server, python client harness): at ~26 queries/s the agent's
report throughput fell ~5x and heartbeats were answered late enough to kick (3001).
"""

import asyncio
import os
import time
import uuid

import httpx
import pytest
import websockets

from conftest import ECHO_DECLARATION, Closed, session_id

pytestmark = pytest.mark.skipif(not os.environ.get("BENCH_SECONDS"), reason="set BENCH_SECONDS to run the benchmark")

AGENT_TOKEN, LOAD_TOKEN = "conf_19", "conf_20"
REPORT_INTERVAL, REPORTS_PER_TASK = 0.05, 100

DETAIL = """query ($id: ID!) { task(id: $id) { id reference latestEventKind args
  events { id kind level returns progress message createdAt }
  children { id latestEventKind events { id kind returns progress createdAt } }
  implementation { id interface agent { id name } action { id name args { key kind } returns { key kind } } } } }"""
LIST = """query { tasks(pagination: {limit: 50}, ordering: {createdAt: DESC}) { id reference latestEventKind
  events { id kind returns progress createdAt } } }"""


def stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    s = sorted(xs)
    return f"n={len(s)} p50={s[len(s) // 2]:.3f} p95={s[int(len(s) * 0.95)]:.3f} max={s[-1]:.3f}"


def counting_declaration() -> dict:
    declared = {**ECHO_DECLARATION, "implementations": [dict(ECHO_DECLARATION["implementations"][0])]}
    implementation = declared["implementations"][0]
    implementation["definition"] = {**implementation["definition"], "kind": "GENERATOR", "key": "bench_count", "name": "Count"}
    implementation["interface"] = "bench_count"
    return {**declared, "hash": f"bench-{uuid.uuid4().hex}"}


class Measured:
    """One agent socket, read by one task that answers heartbeats and routes acks and assigns."""

    def __init__(self, agent) -> None:  # noqa: ANN001
        self.agent = agent
        self.sent_at: dict[int, float] = {}
        self.lags: list[float] = []
        self.heartbeats: list[float] = []
        self.kicks = 0
        self.assigns: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self.acked = 0

    async def read(self) -> None:
        last_heartbeat = None
        try:
            while True:
                frame = await self.agent.receive(timeout=3600)
                now = time.perf_counter()
                if frame["type"] == "HEARTBEAT":
                    await self.agent.send({"type": "HEARTBEAT_ANSWER"})
                    if last_heartbeat is not None:
                        self.heartbeats.append(now - last_heartbeat)
                    last_heartbeat = now
                elif frame["type"] == "JOURNAL_ACK":
                    for pos in [p for p in self.sent_at if p <= frame["pos"]]:
                        self.lags.append(now - self.sent_at.pop(pos))
                    self.acked = max(self.acked, frame["pos"])
                elif frame["type"] == "ASSIGN":
                    await self.assigns.put((frame["task"], now))
        except Closed as closed:
            if closed.code == 3001:
                self.kicks += 1
            raise


async def test_liveness_under_graphql_load(agents, target) -> None:  # noqa: ANN001
    seconds = float(os.environ["BENCH_SECONDS"])
    rates = [float(r) for r in os.environ.get("BENCH_RATES", "0,10,26").split(",")]
    workers = int(os.environ.get("BENCH_CONCURRENCY", "1"))

    agent = await agents()
    init = await agent.register(AGENT_TOKEN, session_id=session_id(), **counting_declaration())
    measured = Measured(agent)
    reader = asyncio.create_task(measured.read())
    journal, pos = f"bench-{uuid.uuid4().hex[:8]}", 0

    async with httpx.AsyncClient(timeout=30) as http:

        async def gql(token: str, query: str, **variables: object) -> dict:
            response = await http.post(target.graphql, json={"query": query, "variables": variables}, headers={"Authorization": f"Bearer {token}"})
            body = response.json()
            assert not body.get("errors"), body["errors"]
            return body["data"]

        for rate in rates:
            stop = time.time() + seconds
            measured.lags.clear(), measured.heartbeats.clear()
            assigns: list[float] = []
            queries: list[float] = []
            reports = 0

            async def stream() -> None:
                try:
                    await stream_tasks()
                except (websockets.ConnectionClosed, Closed, TimeoutError):
                    pass  # kicked: the reader counted it; the rate's numbers still print

            async def stream_tasks() -> None:
                nonlocal pos, reports
                while time.time() < stop and not reader.done():
                    requested = time.perf_counter()
                    data = await gql(
                        AGENT_TOKEN,
                        "mutation ($input: AssignInput!) { assign(input: $input) { id } }",
                        input={"agent": init["agent"], "interface": "bench_count", "args": {"x": 1}, "capture": False, "reference": str(uuid.uuid4())},
                    )
                    task, arrived = await asyncio.wait_for(measured.assigns.get(), 30)
                    assert task == data["assign"]["id"]
                    assigns.append(arrived - requested)
                    frames = [{"type": "STARTED"}] + [{"type": "YIELD", "returns": {"return0": i}} for i in range(REPORTS_PER_TASK)] + [{"type": "COMPLETED"}]
                    for frame in frames:
                        pos += 1
                        measured.sent_at[pos] = time.perf_counter()
                        await agent.send({**frame, "task": task, "pos": pos, "journal_session": journal, "task_step": pos, "agent_ts": time.time()})
                        reports += 1
                        if frame["type"] == "YIELD":
                            await asyncio.sleep(REPORT_INTERVAL)
                        if time.time() >= stop:
                            break

            async def load() -> None:
                if rate <= 0:
                    return
                ids = [t["id"] for t in (await gql(LOAD_TOKEN, "query { tasks(pagination: {limit: 5}, ordering: {createdAt: DESC}) { id } }"))["tasks"]]
                i = 0
                while time.time() < stop:
                    started = time.perf_counter()
                    await (gql(LOAD_TOKEN, DETAIL, id=ids[i % len(ids)]) if ids and i % 2 else gql(LOAD_TOKEN, LIST))
                    queries.append(time.perf_counter() - started)
                    i += 1
                    await asyncio.sleep(max(0.0, 1 / rate - (time.perf_counter() - started)))

            await asyncio.gather(stream(), *[load() for _ in range(workers)])
            await asyncio.sleep(1)  # the last debounce window
            print(
                f"\nRATE {rate}/s x{workers} | reports {reports / seconds:.1f}/s | report lag {stats(measured.lags)} | "
                f"assign {stats(assigns)} | heartbeat spacing {stats(measured.heartbeats)} | kicks {measured.kicks} | "
                f"query {stats(queries)}"
            )
            if reader.done():
                break

    assert measured.kicks == 0, f"the agent was kicked {measured.kicks} time(s) (3001)"
    assert not reader.done(), f"the agent's socket closed: {reader.exception()!r}"

    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
