"""How this server reads "is this agent alive?".

Liveness is ``connected AND a fresh heartbeat``:

* ``connected=False`` is a **definitive negative**: a clean close was observed, or takt's
  sweep revoked the lease.
* ``connected=True`` is only **not-yet-refuted**. A crashed worker never disconnects, so the
  flag can stay stuck True; the heartbeat (``last_seen``) is what makes True trustworthy,
  because it expires on its own, with no writer.

takt writes all of it (``takt/crates/facade/src/persist/leases.rs``): the lease, the
heartbeat renewal and the sweep that revokes a stuck one. This module is the read side, for the
GraphQL ``active`` field and the ``available``/``active`` filters, with the same window takt uses
(``AGENT_STALE_AFTER``: three heartbeat intervals).
"""

import datetime
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone


def stale_after_seconds() -> float:
    """Seconds without a heartbeat after which a ``connected`` agent is presumed dead.

    Defaults to 3× the heartbeat interval — comfortably above ``interval + response_timeout``,
    so a live agent (which refreshes ``last_seen`` every ``AGENT_HEARTBEAT_INTERVAL``) has to
    miss two full heartbeats before it is considered stale.
    """
    return float(settings.AGENT_STALE_AFTER)


def agent_is_live(connected: bool, last_seen: datetime.datetime | None) -> bool:
    """Whether a websocket connection is genuinely alive: connected AND a fresh heartbeat."""
    if not connected or last_seen is None:
        return False
    return last_seen > timezone.now() - timedelta(seconds=stale_after_seconds())


def agent_available_q(prefix: str = "") -> Q:
    """Agents that can receive work now: takt's ``AVAILABLE`` (``backend.rs``), as a ``Q``.

    A webhook agent always can (it never sets ``connected``/``last_seen``); a websocket agent can
    while it is live (``agent_is_live``). ``prefix`` reaches the agent through a relation
    (``"agent__"`` from an implementation). ``Action.assignable`` is the stored form of this rule
    without the heartbeat term (migration ``0006_action_assignable``).
    """
    cutoff = timezone.now() - timedelta(seconds=stale_after_seconds())
    return Q(**{f"{prefix}kind": "WEBHOOK"}) | Q(**{f"{prefix}connected": True, f"{prefix}last_seen__gt": cutoff})
