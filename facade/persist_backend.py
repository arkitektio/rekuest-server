"""The DB-truth backend: ``ModelPersistBackend`` and the process-wide singleton.

The implementation lives in :mod:`facade.persist`, one module per responsibility; this file is the
composition, and the import site everything else already uses.

**Stateless by construction.** This object holds no state. Every deadline it enforces starts at a
DB column (``Agent.last_seen``, ``Task.dispatched_at`` / ``interrupt_at`` / ``last_progress_at``)
and is acted on by a pure, idempotent sweep driven by :mod:`facade.reaper` inside every backend
process. A backend can therefore die at any instant without losing a pending action, and any number
of backends may run concurrently: each task and agent transition is a row-locked claim with exactly
one winner, and the winner alone emits the ``TaskEvent``.

Writes to an agent's liveness columns follow one rule (see :mod:`facade.liveness`): **transitions**
(claim / release / revoke) take a row lock and go through ``save()`` so ``agent_post_save`` fires;
**renewal** (the heartbeat, the only hot path) is a lock-free compare-and-set on ``lease_epoch``
whose rowcount is the answer.
"""

from facade.persist.caller_ops import CallerOpsMixin
from facade.persist.leases import AgentLeaseMixin
from facade.persist.reconcile import ReconcileMixin
from facade.persist.registration import AgentRegistrationMixin
from facade.persist.reports import AgentReportMixin
from facade.persist.state import AgentStateMixin
from facade.persist.transitions import TaskTransitionMixin


class ModelPersistBackend(
    TaskTransitionMixin,  # the kernel: the row-locked claim everything else transitions through
    AgentLeaseMixin,  # who may execute an agent's work (claim / release / revoke / renew)
    ReconcileMixin,  # the sweeps that act on the DB-held deadlines
    AgentReportMixin,  # what an executing agent reports back, and the fence around it
    CallerOpsMixin,  # work an agent originates over its own socket
    AgentStateMixin,  # state patches, snapshots, sessions, lock reports
    AgentRegistrationMixin,  # what the agent declares and what it holds in memory
):
    """Satisfies :class:`facade.ports.PersistBackend`, plus the reconcile surface the reaper drives.

    Composed from mixins rather than delegating to collaborator objects, which four independent
    constraints force: :class:`AgentLeaseMixin` and :class:`ReconcileMixin` call each other in
    *both* directions; five private methods (``_claim_lease_sync``, ``_revoke_lease_sync``,
    ``_unfold_to_higher_order``, ``_claim``, ``_escalate_to_interrupt``) are reached directly from
    tests; this class is constructed with no arguments in dozens of places; and
    ``isinstance(persist_backend, PersistBackend)`` must keep passing. Mixins give all four for
    free — delegation would break every one.
    """


persist_backend = ModelPersistBackend()
