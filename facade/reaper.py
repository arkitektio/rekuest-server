"""The reconciler: every deadline the server enforces fires from here.

The backend is stateless — no deadline lives in a process-local timer. Each one starts at a DB
column and this loop acts on it once it has passed. The loop runs in its own process
(``python manage.py reaper`` — the ``rekuest-reaper`` container), never inside the web
replicas, so a slow sweep cannot stall requests and scaling the web tier does not multiply it:

====================================  =============================  ==========================
sweep                                 deadline starts at             setting
====================================  =============================  ==========================
``reconcile_stale_agents``            ``Agent.last_seen``            ``AGENT_STALE_AFTER``
``reconcile_disconnected_agents``     ``Agent.last_seen``            ``REKUEST_GRACE.DEFAULT``
``provision_service_agents``          ``rekuest.service_agents``     every 5 min (manifest re-read)
``refill_schedules``                  open run of a ``Schedule``     the schedule's own
``fire_triggers``                     unprocessed ``Signal``         (as soon as it arrives)
``dispatch_due_tasks``                ``Task.not_before``            (the task's own)
``reconcile_unpicked_tasks``          ``Task.dispatched_at``         ``…PICKUP_DEADLINE``
``escalate_due_controls``             ``Task.interrupt_at``          ``auto_interrupt`` / ``…CONTROL_DEADLINE``
``reconcile_silent_physical_ops``     ``Task.last_progress_at``      ``…PROGRESS_LEASE``
``expire_disconnected_tasks``         last ``TaskEvent``             ``…DISCONNECTED_EXPIRY``
``sweep_terminal_tasks``              ``Task.finished_at``           ``TASK_RETENTION_SECONDS``
``reembed_stale`` (embeddings)        ``Action.embedding_model``     ``EMBEDDINGS.MODEL``
====================================  =============================  ==========================

The last row is not a deadline but the same discipline: an action whose vector was produced by
another embedding model (or none) is a DB fact, and the row-locked batch re-embed here is what
heals it -- after a model change, after a write while the weights were unreachable, after a
migration on a cold replica. See :mod:`embeddings.healer`.

Consequences, all deliberate:

* **No cron and no state of its own.** A reaper that starts heals whatever an earlier one
  left behind on its first tick, which runs immediately. While none runs, deadlines are late,
  never lost: they are rows, and the next reaper acts on them.
* **A reaper may die at any instant.** Nothing pending is lost with it; the next tick of any
  reaper picks it up. A deadline fires at most ``SWEEP_INTERVAL`` late — the price of having
  no timers.
* **Any number of reapers may run concurrently.** Correctness never depends on who sweeps:
  every transition is a row-locked claim with one winner (``persist_backend``), and sweeps
  step over rows another reaper holds (``skip_locked``). The redis tick token below merely
  keeps N reapers from all scanning the same rows in the same second; if redis is
  unreachable it is skipped and everyone sweeps — wasteful, still correct.

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

from facade import clock, models, redis_keys
from facade.deadlines import sweep_interval_seconds
from facade.persist_backend import persist_backend as _default_backend
from facade.ports import ReconcileBackend
from facade.retention import sweep_terminal_tasks

logger = logging.getLogger(__name__)

# Retention runs on every Nth reaper tick: the deadlines above want responsiveness, the
# retention horizon is measured in days — a slower cadence is plenty and keeps the
# common tick free of the sweep query.
_RETENTION_EVERY_N_TICKS = 60

_PROCESS_ID = uuid.uuid4().hex


def _beat(heartbeat: "Path | None") -> None:
    """Touch the heartbeat file. Never raises: a read-only tmp must not stop the sweeps."""
    if heartbeat is None:
        return
    try:
        heartbeat.touch()
    except OSError:
        logger.debug("Could not touch the reaper heartbeat %s", heartbeat, exc_info=True)


def _sweeps(backend: "ReconcileBackend | None" = None) -> "List[Tuple[str, Callable[[], Awaitable[int]]]]":
    """The ordered sweep steps. Agents before tasks: healing a stuck-connected agent is what
    makes its work visible to the task sweeps of the same tick.

    Typed against :class:`facade.ports.ReconcileBackend` rather than the concrete singleton, so
    what the loop needs from the backend is stated rather than implied.
    """
    persist_backend = backend if backend is not None else _default_backend
    return [
        ("stale agents", persist_backend.reconcile_stale_agents),
        ("disconnected agents", persist_backend.reconcile_disconnected_agents),
        # Before the due-task dispatch: a run whose slot already passed (a reaper that was down)
        # is created and handed over in the same tick.
        # Before the schedules: a freshly provisioned service's default schedules get their
        # first run in the same tick.
        ("service agents", persist_backend.provision_service_agents),
        ("schedules", persist_backend.refill_schedules),
        ("triggers", persist_backend.fire_triggers),
        ("due tasks", persist_backend.dispatch_due_tasks),
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


async def run_forever(heartbeat: "Path | None" = None) -> None:
    """Sweep forever; never let one bad iteration kill the loop. The body of ``manage.py reaper``."""
    tick = 0
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
