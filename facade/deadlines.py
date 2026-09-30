"""The scheduler loop's cadence, from ``REKUEST_GRACE``.

The agent deadlines (grace, pickup, expiry, control escalation) are agentd's; it reads them
from the same configuration block.
"""

from __future__ import annotations

from django.conf import settings


def sweep_interval_seconds() -> float:
    """How often the scheduler loop ticks. Bounds how late a schedule's run is created."""
    return max(0.05, float((getattr(settings, "REKUEST_GRACE", {}) or {}).get("SWEEP_INTERVAL", 5)))
