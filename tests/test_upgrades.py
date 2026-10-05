"""``manage.py upgrade``: what an installer runs between two versions of this service.

Standalone — needs no database; run with ``uv run pytest tests/test_upgrades.py``.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from facade import upgrades


def test_a_move_runs_the_upgrades_of_every_major_it_crosses_into_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """From 5 to 7 is the upgrade into 6, then the one into 7; the one into 8 is not its."""
    ran: list[int] = []
    monkeypatch.setattr(upgrades, "UPGRADES", {7: lambda: ran.append(7), 6: lambda: ran.append(6), 8: lambda: ran.append(8)})

    out = io.StringIO()
    call_command("upgrade", "--from", "5.2.0", "--to", "7.0.1", stdout=out)

    assert ran == [6, 7]
    assert "6, 7" in out.getvalue()


def test_a_move_within_a_major_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minor or a patch brings no upgrade, and neither does going back."""
    ran: list[int] = []
    monkeypatch.setattr(upgrades, "UPGRADES", {6: lambda: ran.append(6)})

    out = io.StringIO()
    call_command("upgrade", "--from", "6.0.0", "--to", "6.3.1", stdout=out)
    call_command("upgrade", "--from", "6.0.0", "--to", "5.2.0", stdout=out)

    assert ran == []
    assert "Nothing to upgrade" in out.getvalue()


def test_an_upgrade_that_fails_fails_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer has to hear it: the previous server is started again on a non-zero exit."""

    def broken() -> None:
        raise RuntimeError("row 3 has no organization")

    monkeypatch.setattr(upgrades, "UPGRADES", {6: broken})
    with pytest.raises(RuntimeError, match="row 3"):
        call_command("upgrade", "--from", "5.2.0", "--to", "6.0.0")


def test_something_that_is_no_version_is_refused() -> None:
    """A label the installer could not read is not guessed at."""
    with pytest.raises(CommandError, match="not a version"):
        call_command("upgrade", "--from", "latest", "--to", "6.0.0")
