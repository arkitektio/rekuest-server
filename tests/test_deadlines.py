"""Unit tests for the reclaim grace-window accessor.

The per-mode dimension is gone with ``AgentMode``: every socket connection is an agent, so
there is exactly one kind of disconnect to grace. What remains is DEFAULT plus the
effect:physical override.
"""

from django.test import override_settings

from facade.deadlines import grace_seconds


@override_settings(REKUEST_GRACE={"DEFAULT": 30, "PHYSICAL": 5})
def test_default_and_physical_resolution():
    assert grace_seconds() == 30
    assert grace_seconds(physical=True) == 5  # physical override wins


@override_settings(REKUEST_GRACE={"DEFAULT": 0})
def test_strict_zero_grace():
    assert grace_seconds() == 0
    # No PHYSICAL configured -> physical falls through to DEFAULT.
    assert grace_seconds(physical=True) == 0
