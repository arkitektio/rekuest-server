"""How this server reads "is this agent alive?".

Liveness is ``connected AND a fresh heartbeat``:

* ``connected=False`` is a **definitive negative**: a clean close was observed, or agentd's
  sweep revoked the lease.
* ``connected=True`` is only **not-yet-refuted**. A crashed worker never disconnects, so the
  flag can stay stuck True; the heartbeat (``last_seen``) is what makes True trustworthy,
  because it expires on its own, with no writer.

agentd writes all of it (``agentd/crates/facade/src/persist/leases.rs``): the lease, the
heartbeat renewal and the sweep that revokes a stuck one. This module is the read side, for the
GraphQL ``active`` field, with the same window agentd uses (``AGENT_STALE_AFTER``: three
heartbeat intervals).
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def stale_after_seconds() -> float:
    """Seconds without a heartbeat after which a ``connected`` agent is presumed dead.

    Defaults to 3× the heartbeat interval — comfortably above ``interval + response_timeout``,
    so a live agent (which refreshes ``last_seen`` every ``AGENT_HEARTBEAT_INTERVAL``) has to
    miss two full heartbeats before it is considered stale.
    """
    return float(getattr(settings, "AGENT_STALE_AFTER", 3 * settings.AGENT_HEARTBEAT_INTERVAL))


def agent_is_live(connected: bool, last_seen) -> bool:
    """Whether a websocket connection is genuinely alive: connected AND a fresh heartbeat."""
    if not connected or last_seen is None:
        return False
    return last_seen > timezone.now() - timedelta(seconds=stale_after_seconds())
