"""The in-process reconciler: every deadline the server enforces fires from here.

The backend is stateless — no deadline lives in a process-local timer. Each one starts at a DB
column and this loop, running inside every backend process, acts on it once it has passed:

====================================  =============================  ==========================
sweep                                 deadline starts at             setting
====================================  =============================  ==========================
``reconcile_stale_agents``            ``Agent.last_seen``            ``AGENT_STALE_AFTER``
``reconcile_disconnected_agents``     ``Agent.last_seen``            ``REKUEST_GRACE.DEFAULT``
``reconcile_unpicked_tasks``          ``Task.dispatched_at``         ``…PICKUP_DEADLINE``
``escalate_due_controls``             ``Task.interrupt_at``          ``auto_interrupt`` / ``…CONTROL_DEADLINE``
``reconcile_silent_physical_ops``     ``Task.last_progress_at``      ``…PROGRESS_LEASE``
``expire_disconnected_tasks``         last ``TaskEvent``             ``…DISCONNECTED_EXPIRY``
``sweep_terminal_tasks``              ``Task.finished_at``           ``TASK_RETENTION_SECONDS``
====================================  =============================  ==========================

Consequences, all deliberate:

* **No management command, no cron, no sidecar.** A backend that starts heals whatever an
  earlier one left behind on its first tick, which runs immediately.
* **A backend may die at any instant.** Nothing pending is lost with it; the next tick of any
  backend picks it up. A deadline fires at most ``SWEEP_INTERVAL`` late — the price of having
  no timers.
* **Any number of backends may run this concurrently.** Correctness never depends on who
  sweeps: every transition is a row-locked claim with one winner (``persist_backend``), and
  sweeps step over rows another backend holds (``skip_locked``). The redis tick token below
  merely keeps N backends from all scanning the same rows in the same second; if redis is
  unreachable it is skipped and everyone sweeps — wasteful, still correct.

Production runs under **daphne**, which has no ASGI ``lifespan``. ``rekuest/asgi.py`` starts the
loop as soon as daphne's event loop runs (``reactor.callWhenRunning``) and, for any other
server, on the first ASGI scope; ``AgentConsumer.connect`` calls it too. All idempotent.
"""

import asyncio
import logging
import random
import uuid
from typing import Awaitable, Callable, List, Optional, Tuple

import redis
from channels.db import database_sync_to_async
from django.conf import settings

from facade import clock, redis_keys
from facade.grace import sweep_interval_seconds
from facade.persist_backend import persist_backend
from facade.retention import sweep_terminal_tasks

logger = logging.getLogger(__name__)

_reaper_task: "Optional[asyncio.Task]" = None

# Retention runs on every Nth reaper tick: the deadlines above want responsiveness, the
# retention horizon is measured in days — a slower cadence is plenty and keeps the
# common tick free of the sweep query.
_RETENTION_EVERY_N_TICKS = 60

_PROCESS_ID = uuid.uuid4().hex


def ensure_reaper_started() -> None:
    """Start the reaper loop once per process; a cheap no-op on every later call."""
    global _reaper_task
    if not getattr(settings, "REKUEST_REAPER_ENABLED", True):
        return  # the test-suite drives the sweeps explicitly, for determinism
    if _reaper_task is not None and not _reaper_task.done():
        return
    _reaper_task = asyncio.ensure_future(_reaper_loop())


def _sweeps() -> "List[Tuple[str, Callable[[], Awaitable[int]]]]":
    """The ordered sweep steps. Agents before tasks: healing a stuck-connected agent is what
    makes its work visible to the task sweeps of the same tick."""
    return [
        ("stale agents", persist_backend.reconcile_stale_agents),
        ("disconnected agents", persist_backend.reconcile_disconnected_agents),
        ("unpicked tasks", persist_backend.reconcile_unpicked_tasks),
        ("due controls", persist_backend.escalate_due_controls),
        ("silent physical ops", persist_backend.reconcile_silent_physical_ops),
        ("expired tasks", persist_backend.expire_disconnected_tasks),
    ]


async def run_sweeps() -> None:
    """One full pass. Every step is isolated: one failing sweep never starves the others.

    A backend whose clock has drifted does not sweep at all. Every sweep here decides whether
    some *other* backend's agent is dead by comparing timestamps, so a skewed clock does not
    produce a late decision but a wrong one — it revokes healthy agents in a loop (see
    :mod:`facade.clock`). Skipping is safe: the deadlines are in the database and any
    correctly-clocked backend will act on them.
    """
    skew = await database_sync_to_async(clock.check_skew)()
    if skew is not None and abs(skew) > clock.max_skew_seconds():
        logger.error(
            "Clock is %.1fs off the database (limit %.1fs) — skipping the sweeps. Check NTP on this host.",
            skew,
            clock.max_skew_seconds(),
        )
        return
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
        from facade.consumers.agent_queue import _sync_pool

        connection = redis.Redis(connection_pool=_sync_pool(settings.AGENT_REDIS_HOST, settings.AGENT_REDIS_PORT))
        return bool(connection.set(redis_keys.key("reaper", "tick"), _PROCESS_ID, nx=True, px=max(1, int(interval * 800))))
    except Exception:
        logger.debug("Reaper tick token unavailable; sweeping anyway.", exc_info=True)
        return True


async def _reaper_loop() -> None:
    """Sweep forever; never let one bad iteration kill the loop."""
    tick = 0
    # A little jitter so backends started together do not tick in lockstep; otherwise the
    # first pass runs right away — it is what heals everything a previous process left behind.
    await asyncio.sleep(random.uniform(0, 0.5))
    while True:
        try:
            interval = sweep_interval_seconds()
            if await asyncio.to_thread(_take_tick_token, interval):
                await run_sweeps()
                tick += 1
                if tick % _RETENTION_EVERY_N_TICKS == 0:
                    # One batch per slow tick; the next one drains any backlog. No-op while
                    # retention is disabled.
                    await database_sync_to_async(sweep_terminal_tasks)(max_batches=1)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error("Reaper iteration failed; continuing.", exc_info=True)
            await asyncio.sleep(1)
