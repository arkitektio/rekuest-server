"""Catalogue what this hub's services host and emit.

    python manage.py catalogue

The job ``catalogue`` of the image (``arkitekt-service run catalogue``); ``migrate`` runs it as
part of its setup. It writes the catalog rows — services, their structures and descriptors,
their signals — from the configuration: an entry of ``rekuest.services`` that carries ``hosts``
is catalogued from that, with no request to anybody, so the catalog is there before the service
(or rekuest itself) runs. An installer runs it again when a service was added or updated,
without restarting rekuest.

An entry without ``hosts`` is asked for its manifest, as takt's periodic pass does. Such a
service may well not be running when this job is (it is run before anything starts): that is
said and is no failure here, since the periodic pass catalogues it once it answers. A
declaration this server was handed and could not write is one: exit 1.

Safe to run again, and beside a running rekuest: one provisioning pass runs at a time (an
advisory lock), and rows are updated in place.
"""

from __future__ import annotations

import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from facade import provisioning

#: How long to wait for a pass another process is running (takt's periodic one takes seconds).
WAIT_SECONDS = 60.0


class Command(BaseCommand):
    help = "Catalogue what the hub's services host and emit: from the config where it says, else from each service's manifest."

    def handle(self, *args: Any, **options: Any) -> None:
        entries = list(settings.SERVICES)
        if not entries:
            self.stdout.write("No services configured (rekuest.services): nothing to catalogue.")
            return

        deadline = time.monotonic() + WAIT_SECONDS
        failed = provisioning.catalogue_services()
        while failed is None:
            if time.monotonic() > deadline:
                raise CommandError(f"Another provisioning pass held the lock for {WAIT_SECONDS:.0f} s; nothing was catalogued.")
            time.sleep(1.0)
            failed = provisioning.catalogue_services()

        refused: list[str] = []
        for entry in entries:
            source = "from the config" if entry.hosts is not None else "from its manifest"
            if entry.name not in failed:
                self.stdout.write(self.style.SUCCESS(f"Catalogued {entry.name} ({source})."))
            elif entry.hosts is not None:
                refused.append(entry.name)
                self.stderr.write(self.style.ERROR(f"Could not catalogue {entry.name} from the config (see the log)."))
            else:
                self.stdout.write(self.style.WARNING(f"Could not reach {entry.name} for its manifest: it is catalogued once it answers."))
        if refused:
            raise CommandError(f"Could not catalogue {', '.join(refused)}.")
