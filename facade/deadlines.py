"""Every deadline the server enforces, resolved from ``REKUEST_GRACE``.

On a disconnect the failure/cascade is delayed by a grace window so a brief blip can
reclaim same-session in-flight work before it fires. That window — like every other deadline
here — is *not* a timer: the moment it starts is a DB column (``Agent.last_seen``,
``Task.dispatched_at``, ``Task.interrupt_at``, ``Task.last_progress_at``) and the periodic
sweep in :mod:`facade.reaper` acts once it has elapsed. Nothing is held in process memory,
so a backend can die at any instant, and any other backend picks the deadline up.
"""

from __future__ import annotations

from django.conf import settings


# The settings key keeps the name ``REKUEST_GRACE`` although this module is no longer only about
# grace: renaming it would touch every deadline test plus ``settings_test.py`` and CONFIG.md, for no
# behavioural gain. The module name is the one readers look things up by; the key is a config
# contract.
def _cfg() -> dict:
    return getattr(settings, "REKUEST_GRACE", {}) or {}


def grace_seconds(*, physical: bool = False) -> float:
    """The reclaim grace window (seconds); ``physical`` overrides for effect:physical work.

    Resolution order: explicit ``PHYSICAL`` override (when ``physical``) → ``DEFAULT``. 0 means
    no grace (strict). Returned as a float so sub-second windows (e.g. in tests) are not
    truncated to 0.

    The per-mode dimension is gone with ``AgentMode``: every socket connection is an agent, so
    there is exactly one kind of disconnect to grace.

    NOTE: no call site currently passes ``physical=`` — effect-awareness lives in the
    fail/reclaim branching of ``persist_backend``, not in the grace timer.
    """
    cfg = _cfg()

    if physical and cfg.get("PHYSICAL") is not None:
        return float(cfg["PHYSICAL"])

    return float(cfg.get("DEFAULT", 0))


def progress_lease_seconds() -> float:
    """The progress-lease window (seconds); 0 disables the lease."""
    return float(_cfg().get("PROGRESS_LEASE", 0))


def sweep_interval_seconds() -> float:
    """How often the in-process reaper ticks. Bounds how late any deadline can fire."""
    return max(0.05, float(_cfg().get("SWEEP_INTERVAL", 5)))


def pickup_deadline_seconds() -> float:
    """How long a dispatched task may go without ANY agent report; 0 disables the watchdog."""
    return float(_cfg().get("PICKUP_DEADLINE", 0))


def disconnected_expiry_seconds() -> float:
    """How long a DISCONNECTED (fate unknown) task stays recoverable; 0 = never expires."""
    return float(_cfg().get("DISCONNECTED_EXPIRY", 0))


def control_deadline_seconds() -> float:
    """How long a cancel may stay unconfirmed before it escalates to an interrupt (and an
    interrupt before it is finalized); 0 disables. A per-request ``auto_interrupt`` wins."""
    return float(_cfg().get("CONTROL_DEADLINE", 0))
