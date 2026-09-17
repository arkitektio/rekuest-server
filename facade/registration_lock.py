"""Serialize the writes a registration makes to rows that are shared across agents.

``implement_agent`` reconciles one agent, but on the way it writes rows that belong to the whole
organization: ``Action`` (shared by every agent of the app), ``Protocol``, ``Collection``,
``StateDefinition``, ``Blok``, ``UICatalog``. Two agents of one fleet registering at the same
moment — a rollout restarting every worker — therefore lock those shared rows in whatever order
their declarations happen to list them, which is a textbook lock-order inversion: A holds action
``x`` and wants protocol ``p`` while B holds ``p`` and wants ``x``. Postgres breaks the cycle by
aborting one side, so the symptom is registrations failing intermittently during a deploy.

Sorting the declarations cannot fix it (the shared rows are interleaved *between* the actions), so
registration takes one lock per organization instead. It is a **transaction-level** advisory lock:
released at commit or rollback, with nothing to clean up if the backend dies mid-registration.

Cost: registrations of one organization serialize. They are sub-second, and this replaces a class
of failure that only appears when it hurts most — during a fleet-wide restart.
"""

from django.db import connection

# Arbitrary but fixed: the first half of a two-key advisory lock, so this namespace cannot collide
# with another feature's advisory locks on the same database.
REGISTRATION_LOCK_CLASS = 0x52454B55  # "REKU"


def lock_organization(organization) -> None:
    """Take the registration lock for ``organization``, waiting if another backend holds it."""
    organization_id = getattr(organization, "pk", organization)
    if organization_id is None or connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [REGISTRATION_LOCK_CLASS, int(organization_id)])
