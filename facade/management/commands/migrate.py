"""``migrate``, serialized across replicas.

Every replica runs ``python manage.py migrate`` when its container boots (``run.sh``) — there
is deliberately no separate migration job to operate. Django does not serialize that: two
replicas starting together both read ``django_migrations``, both decide the same migration is
unapplied and both run it. One of them dies on ``relation already exists`` (or deadlocks on the
DDL), and under ``set -e`` that replica never starts.

This command shadows Django's own (an app's command overrides a core one, the way daphne
overrides ``runserver``), so ``manage.py migrate`` is safe wherever it is typed. It takes a
Postgres **session-level advisory lock** on the connection the migration runs on, then hands
over to the stock implementation:

* the first replica migrates; the others block here, then build their migration plan — which
  happens inside ``handle``, i.e. *after* the lock — find nothing to apply, and start serving;
* the lock also serializes ``post_migrate`` (permissions, content types), which races too;
* session-level, not transaction-level: a migrate spans many transactions (some non-atomic);
* if the holder crashes its session ends and Postgres releases the lock — nothing to clean up;
* advisory locks are per database, so one constant key is safe on a cluster shared by services.

Not compatible with a transaction-pooling pgbouncer (session state is not preserved there).
"""

from __future__ import annotations

import hashlib
import time

from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrate
from django.db import DEFAULT_DB_ALIAS, connections

# Stable signed 64-bit key, derived rather than hand-picked so it cannot collide by accident.
LOCK_KEY = int.from_bytes(hashlib.sha256(b"django:migrate").digest()[:8], "big", signed=True)


class Command(DjangoMigrate):
    help = DjangoMigrate.help + " Serialized across replicas by a Postgres advisory lock."

    def add_arguments(self, parser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--no-lock", action="store_true", help="Do not take the cross-replica advisory lock.")
        parser.add_argument("--lock-timeout", type=int, default=900, help="Seconds to wait for another replica's migrate before giving up (default 900).")

    def handle(self, *args, **options):
        connection = connections[options.get("database") or DEFAULT_DB_ALIAS]
        if options.get("no_lock") or connection.vendor != "postgresql":
            return super().handle(*args, **options)

        connection.ensure_connection()
        deadline = time.monotonic() + options["lock_timeout"]
        waited = 0
        while True:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
                if cursor.fetchone()[0]:
                    break
            if time.monotonic() >= deadline:
                raise CommandError("Timed out waiting for another replica to finish migrating.")
            if waited % 10 == 0:
                self.stdout.write("Another replica is migrating — waiting for it to finish…")
            waited += 1
            time.sleep(1)

        try:
            return super().handle(*args, **options)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
