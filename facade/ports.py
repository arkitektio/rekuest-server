"""The typed port the real-time layer depends on (instead of the concrete singleton).

The DB is the source of truth; :class:`PersistBackend` describes the persistence seam the
transport-agnostic protocol code talks to, so the WebSocket consumer, the HTTP intake, and
the message router depend on an interface rather than on ``ModelPersistBackend`` directly.
(Outbound delivery is a small set of typed functions in :mod:`facade.transport`, not a
class, so it needs no Protocol here.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from facade import messages, models


@dataclass
class LeaseClaim:
    """The outcome of an executor's attempt to claim an agent's write-lease.

    ``claimed`` False means a genuinely-live incumbent holds the lease and ``force`` was not
    set — the transport must reject the registration. ``epoch`` is the fencing token the
    winning connection presents on every later lease renewal; it is ``None`` on a failed claim.
    ``displaced_incumbent`` says a prior connection existed (live or stale) and should be
    kicked — an optimization, not a correctness dependency: an unreachable prior connection
    fences itself as soon as its next heartbeat renewal fails.
    """

    claimed: bool
    epoch: Optional[int] = None
    tasks: List["models.Task"] = field(default_factory=list)
    displaced_incumbent: bool = False


@runtime_checkable
class PersistBackend(Protocol):
    """The DB-truth backend surface called by the transports (WS + HTTP intake).

    Every method persists/reads the authoritative Postgres rows. ``agent_id`` is the Agent
    pk (int); wire ids on ``messages`` are strings (the serialization boundary).
    """

    # --- connection lifecycle (agent liveness) -------------------------------- #
    # ``on_agent_connected`` both *gates* and *claims* the agent singleton, so it returns a
    # ``LeaseClaim`` rather than a task list: whether the claim succeeded, the fencing token to
    # renew with, the in-flight work to re-inquire, and whether a prior connection needs kicking.
    async def on_agent_connected(self, agent_id: int, connection_id: str | None = ..., session_id: str | None = ..., force: bool = ...) -> LeaseClaim: ...
    async def renew_agent_lease(self, agent_id: int, lease_epoch: int) -> bool: ...
    async def on_agent_disconnected(self, agent_id: int, connection_id: str | None = ...) -> None: ...

    async def get_or_create_caller_id(self, agent_id: int) -> str: ...

    # The delivery-time fence: is an Assign for this task still worth sending? (The server may
    # have finalized the task while its frame waited in an offline agent's queue.)
    async def is_task_open(self, task_id: str) -> bool: ...

    # Fencing at delivery time: does this connection still hold the agent's lease?
    async def holds_lease(self, agent_id: int, lease_epoch: int) -> bool: ...

    # --- sub-assignment (dependent work only; roots come from GraphQL) --------- #
    async def on_caller_assign(
        self,
        agent_id: int,
        message: messages.AssignRequest,
        connection_id: str | None = ...,
        session_id: str | None = ...,
    ) -> Tuple["models.Task", bool]: ...

    # --- caller lifecycle controls (request phase) ---------------------------- #
    async def on_caller_cancel(self, agent_id: int, message: messages.CancelRequest, *, connection_id: str | None = ..., session_id: str | None = ...) -> "models.Task": ...
    async def on_caller_interrupt(self, agent_id: int, message: messages.InterruptRequest, *, connection_id: str | None = ..., session_id: str | None = ...) -> "models.Task": ...
    async def on_caller_pause(self, agent_id: int, message: messages.PauseRequest, *, connection_id: str | None = ..., session_id: str | None = ...) -> "models.Task": ...
    async def on_caller_resume(self, agent_id: int, message: messages.ResumeRequest, *, connection_id: str | None = ..., session_id: str | None = ...) -> "models.Task": ...

    # --- lifecycle confirmations (confirm phase) ------------------------------ #
    async def on_agent_started(self, agent_id: int, message: messages.Started) -> None: ...
    async def on_agent_interrupted(self, agent_id: int, message: messages.Interrupted) -> None: ...
    async def on_agent_paused(self, agent_id: int, message: messages.Paused) -> None: ...
    async def on_agent_resumed(self, agent_id: int, message: messages.Resumed) -> None: ...

    # --- agent-reported events ------------------------------------------------ #
    async def on_agent_log(self, agent_id: int, message: messages.Log) -> None: ...
    async def on_agent_yield(self, agent_id: int, message: messages.Yield) -> None: ...
    async def on_agent_progress(self, agent_id: int, message: messages.Progress) -> None: ...
    async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None: ...
    async def on_agent_error(self, agent_id: int, message: messages.Failed) -> None: ...
    async def on_agent_critical(self, agent_id: int, message: messages.Critical) -> None: ...
    async def on_agent_cancelled(self, agent_id: int, message: messages.Cancelled) -> None: ...
    async def on_agent_state_patch(self, agent_id: int, message: messages.StatePatch) -> None: ...
    async def on_agent_state_snapshot(self, agent_id: int, message: messages.StateSnapshot) -> None: ...
    async def on_agent_session_init(self, agent_id: int, message: messages.SessionInit) -> None: ...

    # --- distributed locks (acquire / release) -------------------------------- #
    async def on_agent_lock(self, agent_id: int, message: messages.Lock) -> None: ...
    async def on_agent_unlock(self, agent_id: int, message: messages.Unlock) -> None: ...


@runtime_checkable
class ReconcileBackend(Protocol):
    """The sweep surface :mod:`facade.reaper` drives — the *other* seam on the same backend.

    Split from :class:`PersistBackend` because the two have different callers and different
    contracts. A transport (websocket, HTTP intake) reacts to what one agent just said; these
    answer "what has fallen past its deadline?" across the whole table, from any backend, and each
    returns how many rows it acted on so the loop can log it.

    Every one is idempotent and safe to run concurrently with itself in another process: candidates
    are scanned with ``skip_locked`` and each transition is a row-locked claim with one winner. That
    is what lets the reaper run in every replica with no leader election.
    """

    # Agents first: healing a stuck-connected agent is what makes its work visible to the task
    # sweeps of the same tick.
    async def reconcile_stale_agents(self) -> int: ...
    async def reconcile_disconnected_agents(self) -> int: ...

    async def reconcile_unpicked_tasks(self, limit: int = ...) -> int: ...
    async def escalate_due_controls(self, limit: int = ...) -> int: ...
    async def reconcile_silent_physical_ops(self, limit: int = ...) -> int: ...
    async def expire_disconnected_tasks(self, limit: int = ...) -> int: ...
