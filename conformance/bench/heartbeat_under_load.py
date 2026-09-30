"""PARKED: the liveness benchmark, as first run from rekuest's integration harness.

It imports rekuest's test conftest (build_fresh_rekuest, rekuest_port); Phase 1 ports it onto
this suite's RawAgent so it measures any target. Numbers it produced (2026-09-30, Python server):
at ~26 concurrent UI queries/s agent-report throughput fell ~5x (167 vs ~550 items/30 s).
"""

import asyncio
import os
import time
from collections.abc import AsyncGenerator

import aiohttp
import pytest
from dokker import Deployment

from .conftest import CONNECT_TIMEOUT, build_fresh_rekuest, rekuest_port

DETAIL = """query ($id: ID!) { task(id: $id) { id reference latestEventKind args
  events { id kind level returns progress message createdAt }
  children { id latestEventKind events { id kind returns progress createdAt } }
  implementation { id interface agent { id name } action { id name args { key kind } returns { key kind } } } } }"""
LIST = """query { tasks(pagination: {limit: 50}, ordering: {createdAt: DESC}) { id reference latestEventKind
  events { id kind returns progress createdAt } } }"""


def _stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    s = sorted(xs)
    return f"n={len(s)} p50={s[len(s) // 2]:.3f} p95={s[int(len(s) * 0.95)]:.3f} max={s[-1]:.3f}"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_heartbeat_under_graphql_load(deployment: Deployment) -> None:
    port = rekuest_port(deployment)
    app = build_fresh_rekuest(deployment, token="standalone_token")

    async def count(to: int, delay: float) -> AsyncGenerator[str, None]:
        """Count"""
        for i in range(to):
            await asyncio.sleep(delay)
            yield f"{i} of {to}"

    app.register(count)
    rates = [float(r) for r in os.environ.get("EXP_RATES", "0,3,10").split(",")]
    seconds = float(os.environ.get("EXP_SECONDS", "30"))

    async with app as app:
        await app.aconnect(timeout=CONNECT_TIMEOUT)
        loop_task = asyncio.create_task(app.aloop())
        impl = await app.amy_implementation_at("count")

        async with aiohttp.ClientSession(headers={"Authorization": "Bearer standalone_token"}) as http:

            async def gql(query: str, variables: dict) -> float:
                t = time.perf_counter()
                async with http.post(f"http://localhost:{port}/graphql", json={"query": query, "variables": variables}) as r:
                    body = await r.json()
                    assert "errors" not in body, body
                return time.perf_counter() - t

            for rate in rates:
                stop = time.time() + seconds
                gaps: list[float] = []
                probes: list[float] = []
                queries: list[float] = []

                starts: list[float] = []

                async def stream() -> None:
                    while time.time() < stop:
                        last, first = time.perf_counter(), True
                        async for _ in app.aiterate(impl, to=100, delay=0.05):
                            now = time.perf_counter()
                            (starts if first else gaps).append(now - last)
                            last, first = now, False
                            if time.time() >= stop:
                                break

                async def probe() -> None:
                    while time.time() < stop:
                        t = time.perf_counter()
                        async with http.get(f"http://localhost:{port}/ht") as r:
                            await r.read()
                        probes.append(time.perf_counter() - t)
                        await asyncio.sleep(0.1)

                async def load() -> None:
                    if rate <= 0:
                        return
                    ids = [t["id"] for t in (await (await http.post(f"http://localhost:{port}/graphql", json={"query": "query { tasks(pagination: {limit: 5}, ordering: {createdAt: DESC}) { id } }"})).json())["data"]["tasks"]]
                    i = 0
                    while time.time() < stop:
                        started = time.perf_counter()
                        queries.append(await gql(DETAIL, {"id": ids[i % len(ids)]}) if ids and i % 2 else await gql(LIST, {}))
                        i += 1
                        await asyncio.sleep(max(0.0, 1 / rate - (time.perf_counter() - started)))

                workers = int(os.environ.get("EXP_CONCURRENCY", "1"))
                await asyncio.gather(stream(), probe(), *[load() for _ in range(workers)])
                print(f"\nRATE {rate}/s | in-stream gap {_stats(gaps)} | task start {_stats(starts)} | /ht {_stats(probes)} | query {_stats(queries)}")

        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
