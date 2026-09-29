"""Unit tests for the reclaim grace-window accessor."""

from django.test import override_settings

from facade.deadlines import grace_seconds


@override_settings(REKUEST_GRACE={"DEFAULT": 30})
def test_the_default_window():
    assert grace_seconds() == 30


@override_settings(REKUEST_GRACE={"DEFAULT": 0})
def test_strict_zero_grace():
    assert grace_seconds() == 0
