"""What this service does to its own data when a deployment moves from one version to another.

Schema changes are Django migrations, and those run in the migration job, before the server
starts. This is for what a migration cannot express, or must not do while the previous release
still serves: a rewrite that needs the old and the new code not to overlap, a one-off pass over
rows another program (takt) also writes. An installer runs it once, between stopping the old
server and starting the new one::

    python manage.py upgrade --from 5.2.0 --to 6.0.0

Each entry is keyed by the **major it leads to** and runs when a move crosses into that major:
from 5.x to 6.x runs ``UPGRADES[6]``; from 5.x to 7.x runs ``UPGRADES[6]`` and then
``UPGRADES[7]``. A move within a major runs nothing. An upgrade has to be safe to run twice:
an installer that stops half way runs it again.

There are none yet.
"""

from __future__ import annotations

from collections.abc import Callable

#: The major a move crosses into, to what has to happen on the way.
UPGRADES: dict[int, Callable[[], None]] = {}


def major(version: str) -> int:
    """The major of a version as release tags spell it: ``6.0.0``, ``6.1.0-rc.1``, ``6``."""
    try:
        return int(version.strip().removeprefix("v").split(".")[0])
    except ValueError:
        raise ValueError(f"{version!r} is not a version (expected something like 6.0.0)") from None


def crossed(left: str, reached: str) -> list[int]:
    """The majors with an upgrade that a move from ``left`` to ``reached`` crosses into, in order."""
    start, end = major(left), major(reached)
    return [step for step in sorted(UPGRADES) if start < step <= end]


def run(left: str, reached: str) -> list[int]:
    """Run every upgrade the move crosses, oldest first; the majors that had one."""
    steps = crossed(left, reached)
    for step in steps:
        UPGRADES[step]()
    return steps
