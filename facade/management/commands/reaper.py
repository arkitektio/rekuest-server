"""Run the reconciler loop (:mod:`facade.reaper`) — the ``rekuest-reaper`` container's process.

    python manage.py reaper            # sweep forever
    python manage.py reaper --check    # healthcheck: exit 1 unless the heartbeat is fresh

The loop holds no state: stop it, run two, restart it — every deadline is a database row, and
whichever reaper ticks next acts on it. The web replicas never sweep.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from django.core.management.base import BaseCommand

from facade import reaper
from facade.deadlines import sweep_interval_seconds

DEFAULT_HEARTBEAT = "/tmp/rekuest-reaper.heartbeat"


class Command(BaseCommand):
    help = "Run the reaper loop (every deadline, schedule and delayed task fires from it)."

    def add_arguments(self, parser):
        parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT, help="File touched every iteration (default: %(default)s).")
        parser.add_argument("--check", action="store_true", help="Exit 0 if the heartbeat is fresh, 1 otherwise — for a container healthcheck.")
        parser.add_argument("--max-age", type=float, default=None, help="With --check: the stalest heartbeat still healthy, in seconds (default: max(30, 6 sweep intervals)).")

    def handle(self, *args, heartbeat: str, check: bool, max_age: float | None, **options):
        path = Path(heartbeat)
        if check:
            limit = max_age if max_age is not None else max(30.0, 6 * sweep_interval_seconds())
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                self.stderr.write(f"No reaper heartbeat at {path}")
                sys.exit(1)
            if age > limit:
                self.stderr.write(f"Reaper heartbeat is {age:.0f}s old (limit {limit:.0f}s)")
                sys.exit(1)
            self.stdout.write(f"Reaper heartbeat {age:.1f}s old")
            return

        self.stdout.write(f"Reaper running (heartbeat {path})")
        asyncio.run(reaper.run_forever(heartbeat=path))
