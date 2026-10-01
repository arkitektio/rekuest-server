"""Single source of truth for "is this websocket agent alive?".

Liveness is read as ``connected AND a fresh heartbeat``, and the asymmetry is deliberate:

* ``connected=False`` is a **definitive negative** — somebody observed a clean close (or the
  sweep revoked the lease), so the agent is instantly, correctly not-live.
* ``connected=True`` is only **not-yet-refuted**. It is written on connect and flipped by the
  disconnect handler, which runs only on a clean close — a crashed/SIGKILLed worker never
  disconnects, so the flag can stay stuck True forever. The heartbeat lease (``last_seen``) is
  what makes True trustworthy: it expires on its own, with no writer.

So the *read* predicate needs no repair; correctness lives in the **write discipline** on the
other side (``facade.persist_backend``):

* **Transitions** — claim (connect), release (disconnect), revoke (sweep) — take a row lock
  (``select_for_update``) and go through ``Model.save()`` so ``agent_post_save`` fires and the
  GraphQL agent feeds see the change.
* **Renewal** — the heartbeat, the only hot path (once per ``AGENT_HEARTBEAT_INTERVAL`` per
  agent) — is a lock-free compare-and-set on ``lease_epoch``; its rowcount *is* the answer to
  "am I still the owner?". A displaced or revoked connection matches no row and closes itself.

Every socket connection is an agent and holds that agent's lease, so every heartbeat renews.
(There used to be caller/observer connection modes sharing the same ``Agent`` row — identity is
``client``/``user``/``organization`` — whose heartbeats touched ``last_seen`` and let an open
dashboard forge executor liveness indefinitely. Those modes are gone: users originate work only
through the GraphQL ``assign`` mutation.)

Note on stuck flags: the heartbeat ping/pong already closes half-open sockets (no answer within
``AGENT_HEARTBEAT_RESPONSE_TIMEOUT`` → ``HEARTBEAT_NOT_RESPONDED_CODE``). The residual causes of
a stuck ``connected=True`` are **displacement** and **hard worker death** — which is why the
fix is a fencing token plus a sweep, not a longer timeout.

All liveness timestamps are written with the *application* clock (``timezone.now()``) and
compared against it, so the whole predicate lives in one clock domain and only needs the app
servers to be NTP-synced. Do not mix in the database clock (``Now()``): that would add an
app-vs-DB skew axis on top of the app-vs-app one.

Historically each call site rolled its own staleness window (20 s / 1 min / 5 min); they are
unified here behind one ``AGENT_STALE_AFTER`` window so the reconnect gate, the availability
query, the GraphQL ``active`` field, and the healing reaper all agree.

This module is a LEAF: it imports only Django. ``agent_protocol`` cannot import ``backend``
(cycle ``backend → async_consumer → agent_protocol``), so the shared helper lives here where
both — and the models/types/management-command layers — can import it safely.
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
