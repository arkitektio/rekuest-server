"""The provisioning pass: what takt asks for at start and every five minutes (:mod:`facade.upkeep`).

Its first half alone is also a job of the image (``manage.py catalogue``, run by ``migrate`` as
setup and by an installer whenever it likes): :func:`catalogue_services`.

Two independent things happen in it, and neither reads what the other wrote:

* the **catalog**: each configured service's structures and signals (:mod:`facade.service_catalog`);
* the **hook agents**: each configured hook agent, in every organization (:mod:`facade.hook_agents`).

They share only the lock: one pass at a time, across every replica.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from authentikate.models import Organization
from django.db import connection

from facade import hook_agents, service_catalog

# One provisioning pass at a time, across every replica: rows are found-or-created, and two
# passes at once would both create them. A session-level advisory lock, not a transaction: a
# pass asks takt to write rows that must see what this one already committed.
PROVISION_LOCK_KEY = int.from_bytes(hashlib.sha256(b"rekuest:provisioning").digest()[:8], "big", signed=True)


@contextmanager
def _locked() -> Iterator[bool]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [PROVISION_LOCK_KEY])
        held = cursor.fetchone()[0]
    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [PROVISION_LOCK_KEY])


@dataclass(frozen=True)
class Failed:
    """The names a provisioning pass could not do."""

    services: list[str]
    hook_agents: list[str]


def provision_all() -> Failed | None:
    """Catalogue the services and provision the hook agents; which of each could not be.

    ``None`` when another replica is provisioning right now (nothing was done here).
    """
    with _locked() as held:
        if not held:
            return None
        return Failed(services=service_catalog.catalogue_all(), hook_agents=hook_agents.provision_all())


def catalogue_services() -> list[str] | None:
    """The catalog alone, under the same lock: the services that could not be catalogued.

    What the ``catalogue`` job runs (``manage.py catalogue``): it needs the database and
    nothing else, so it can run before anything serves. ``None`` when another pass holds the lock.
    """
    with _locked() as held:
        if not held:
            return None
        return service_catalog.catalogue_all()


def provision_hook_agents(organizations: list[Organization]) -> list[str] | None:
    """The hook agents of just these organizations (one that was just created)."""
    with _locked() as held:
        if not held:
            return None
        return hook_agents.provision_all(organizations)
