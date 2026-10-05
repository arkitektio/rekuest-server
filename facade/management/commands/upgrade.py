"""Run what this release has to do to its data when a deployment moves to it from another.

    python manage.py upgrade --from 5.2.0 --to 6.0.0

For an installer, between stopping the previous server and starting this one. Exits 0 when
the upgrades ran or there were none, non-zero when one failed — the installer then starts the
previous server again. See ``facade.upgrades``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser

from facade import upgrades


class Command(BaseCommand):
    """``manage.py upgrade``: the data upgrades between two versions of this service."""

    help = "Run what this release has to do to its data when moving to it from another version."

    def add_arguments(self, parser: CommandParser) -> None:
        """The version being left and the one being moved to."""
        parser.add_argument("--from", dest="left", required=True, help="The version the deployment ran, e.g. 5.2.0.")
        parser.add_argument("--to", dest="reached", required=True, help="The version it moves to, e.g. 6.0.0.")

    def handle(self, *args: object, **options: object) -> None:
        """Run the upgrades the move crosses, and say which."""
        left, reached = str(options["left"]), str(options["reached"])
        try:
            ran = upgrades.run(left, reached)
        except ValueError as error:
            raise CommandError(str(error)) from error
        if not ran:
            self.stdout.write(f"Nothing to upgrade between {left} and {reached}.")
            return
        self.stdout.write(f"Upgraded from {left} to {reached}: {', '.join(str(step) for step in ran)}.")
