"""Guard the one assumption the liveness model makes across backends: a shared clock.

``facade.liveness`` compares application clocks — one backend writes ``Agent.last_seen`` with its
``timezone.now()``, another's sweep compares it against its own. That is fine on one host and
false the moment backends sit on different machines with drifting clocks.

The failure is not a blip but a loop. A backend whose clock runs ahead sees a perfectly healthy
agent as stale, revokes its lease (bumping ``lease_epoch``), and fails its in-flight work; the
agent's next heartbeat cannot renew, so it reconnects — and is revoked again. The margin is
exactly how much older than the stale window a *fresh* heartbeat can look:

    tolerated skew = AGENT_STALE_AFTER − AGENT_HEARTBEAT_INTERVAL − AGENT_HEARTBEAT_RESPONSE_TIMEOUT

split between two backends drifting in opposite directions, hence the halving below. Rather than
trust operators to notice, a backend measures itself against the database clock — the one clock
every backend already shares — and, if it is out, refuses to sweep and reports unhealthy so the
orchestrator takes it out of rotation. Reading is unaffected: a skewed backend may serve queries,
it just must not be the one deciding that other backends' agents are dead.
"""

from __future__ import annotations

import dataclasses
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.utils import timezone
from health_check.base import HealthCheck
from health_check.exceptions import HealthCheckException

logger = logging.getLogger(__name__)


def max_skew_seconds() -> float:
    """How far this backend's clock may be from the database's before it stops sweeping."""
    stale_after = float(getattr(settings, "AGENT_STALE_AFTER", 30))
    interval = float(getattr(settings, "AGENT_HEARTBEAT_INTERVAL", 10))
    response_timeout = float(getattr(settings, "AGENT_HEARTBEAT_RESPONSE_TIMEOUT", 5))
    # Halved: two backends can drift in opposite directions, so each may spend half the budget.
    return max(1.0, (stale_after - interval - response_timeout) / 2)


def measure_skew() -> float:
    """Seconds this process's clock is ahead of (positive) or behind (negative) the database's."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT now()")
        database_now = cursor.fetchone()[0]
    return (timezone.now() - database_now).total_seconds()


def check_skew() -> float | None:
    """The measured skew, or None when it could not be measured (never a reason to block work)."""
    try:
        return measure_skew()
    except Exception:
        logger.debug("Could not measure clock skew", exc_info=True)
        return None


@dataclasses.dataclass
class ClockSkewHealthCheck(HealthCheck):
    """Report unhealthy while this backend's clock disagrees with the database's.

    Wired into ``/ht`` so an orchestrator or load balancer stops sending work to a host whose
    clock would make it mis-judge every other backend's agents. A skew we cannot measure is not
    a failure — the database check next to this one covers a database that is unreachable.
    """

    async def run(self) -> None:
        skew = await sync_to_async(check_skew)()
        if skew is None:
            return
        limit = max_skew_seconds()
        if abs(skew) > limit:
            raise HealthCheckException(f"Clock is {skew:.1f}s off the database clock (limit {limit:.1f}s); check NTP on this host.")
