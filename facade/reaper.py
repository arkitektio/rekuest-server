"""The server's own background loop: service agents and embeddings.

It runs in its own process (``python manage.py reaper``, the ``rekuest-reaper`` container),
never inside the web replicas, so a slow pass cannot stall requests and scaling the web tier
does not multiply it:

====================================  =============================  ==========================
step                                  acts on                        cadence
====================================  =============================  ==========================
``provision_service_agents``          ``rekuest.service_agents``     every 5 min (manifest re-read)
``reembed_stale`` (embeddings)        ``Action.embedding_model``     every tick
====================================  =============================  ==========================

Everything that creates, moves or prunes tasks is agentd's, and every agentd replica runs it:
the agent sweeps, schedules, triggers and retention. What stays here needs this server: service
manifests are fetched and registered through GraphQL-side models, and embeddings run the model.

An action whose vector was produced by another embedding model (or none) is a database fact, and
the row-locked batch re-embed here is what heals it: after a model change, after a write while
the weights were unreachable, after a migration on a cold replica. See :mod:`embeddings.healer`.

Consequences, all deliberate:

* **No state of its own.** A loop that starts does on its first tick, which runs immediately,
  whatever an earlier one left undone.
* **It may die at any instant.** Nothing pending is lost with it; the next tick of any loop
  picks it up.
* **Any number may run concurrently.** Provisioning is idempotent and the re-embed is
  row-locked. The redis tick token below merely keeps N loops from doing the same work in the
  same second; if redis is unreachable it is skipped and everyone runs: wasteful, still correct.

Every iteration touches a heartbeat file; ``manage.py reaper --check`` (the container's
healthcheck) fails once it is older than a few intervals, so a wedged loop is visible.
"""

import asyncio
import logging
import random
import uuid
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple

import redis
from channels.db import database_sync_to_async
from django.conf import settings
from embeddings.healer import reembed_stale

from facade import models, redis_keys, service_agents
from facade.deadlines import sweep_interval_seconds

logger = logging.getLogger(__name__)

_PROCESS_ID = uuid.uuid4().hex


def _beat(heartbeat: "Path | None") -> None:
    """Touch the heartbeat file. Never raises: a read-only tmp must not stop the sweeps."""
    if heartbeat is None:
        return
    try:
        heartbeat.touch()
    except OSError:
        logger.debug("Could not touch the reaper heartbeat %s", heartbeat, exc_info=True)


def _sweeps() -> "List[Tuple[str, Callable[[], Awaitable[int]]]]":
    """The ordered steps."""
    return [("service agents", database_sync_to_async(service_agents.provision_all))]


async def run_sweeps() -> None:
    """One full pass. Every step is isolated: one failing sweep never starves the others."""
    for name, sweep in _sweeps():
        try:
            acted = await sweep()
            if acted:
                logger.info("Reaper: %s → %s", name, acted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Reaper sweep %r failed; continuing.", name, exc_info=True)


def _take_tick_token(interval: float) -> bool:
    """Whether this backend sweeps this tick — work de-duplication across backends, nothing more.

    ``SET NX PX``: the first backend to tick takes the token for most of one interval; the
    others skip. The holder dying costs at most one interval. Any redis problem → sweep anyway.
    """
    try:
        connection = redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)
        # Not agentd's ``reaper:tick``: the two loops do different things and must not skip each
        # other's ticks.
        return bool(connection.set(redis_keys.key("scheduler", "tick"), _PROCESS_ID, nx=True, px=max(1, int(interval * 800))))
    except Exception:
        logger.debug("Reaper tick token unavailable; sweeping anyway.", exc_info=True)
        return True


async def run_forever(heartbeat: "Path | None" = None) -> None:
    """Sweep forever; never let one bad iteration kill the loop. The body of ``manage.py reaper``."""
    # A little jitter so reapers started together do not tick in lockstep; otherwise the
    # first pass runs right away — it is what heals everything a previous process left behind.
    await asyncio.sleep(random.uniform(0, 0.5))
    while True:
        try:
            interval = sweep_interval_seconds()
            # Beaten whether or not this reaper holds the tick token: a sibling sweeping this
            # second is healthy work, not a wedged loop.
            _beat(heartbeat)
            if await asyncio.to_thread(_take_tick_token, interval):
                await run_sweeps()
                # Stale embeddings: an indexed no-op when there are none, a few hundred rows a
                # tick when there are (model change, cold write). Off the event loop: the
                # model runs on the CPU of this process.
                await database_sync_to_async(reembed_stale)(models.Action, max_batches=5)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error("Reaper iteration failed; continuing.", exc_info=True)
            await asyncio.sleep(1)
