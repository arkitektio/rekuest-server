"""Agent-event handling for probes — the redis twin of ``persist_backend``.

The message router sends every task-carrying agent message whose id is a probe id here
instead of to the DB backend. No handler touches SQL: state transitions land in the
:mod:`facade.probes.store` hash and the event is fanned out payload-carrying over the
channel layer (topic ``probe_events_{id}``) — subscribers relay it with zero lookups.

Terminal dedup works like the Task path's ``select_for_update`` claim, but via
``HSETNX done``: agents retry terminal reports until acked, and across all daphne
processes exactly one claim wins; losers drop the resend silently. This module must not
import ``facade.backend`` (the router is imported underneath it — cycle).
"""

from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone

from facade import enums, messages
from facade.probes.store import ProbeStore, get_probe_store
from facade.channel_events import ProbeEventBroadcast
from facade.channels import probe_event_channel

logger = logging.getLogger(__name__)


def probe_events_topic(probe_id: str) -> str:
    return f"probe_events_{probe_id}"


def probe_topics(probe_id: str, caller: Optional[str], origin: str) -> list[str]:
    """The channel topics one probe event fans out to.

    Every probe streams on its own ``probe_events_{id}`` topic (the GraphQL
    subscription). An agent-originated probe additionally mirrors onto the requester's
    ``task_caller_{caller}`` topic — the requesting agent's socket already sits in that
    group from registration, so its ``…Event`` mirrors arrive with no membership race.
    """
    topics = [probe_events_topic(probe_id)]
    if origin == "agent" and caller:
        topics.append(f"task_caller_{caller}")
    return topics


async def _publish(
    probe_id: str,
    kind: str,
    seq: int,
    *,
    message: Optional[str] = None,
    progress: Optional[int] = None,
    returns: Optional[dict] = None,
    level: Optional[str] = None,
    caller: Optional[str] = None,
    origin: str = "graphql",
) -> None:
    await probe_event_channel.abroadcast(
        ProbeEventBroadcast(
            probe=probe_id,
            kind=kind,
            seq=seq,
            message=message,
            progress=progress,
            returns=returns,
            level=level,
            created_at=timezone.now(),
        ),
        probe_topics(probe_id, caller, origin),
    )


class ProbeEventBackend:
    def __init__(self, store: Optional[ProbeStore] = None) -> None:
        self._store = store

    @property
    def store(self) -> ProbeStore:
        return self._store or get_probe_store()

    # ------------------------------------------------------------------ #
    # non-terminal reports
    # ------------------------------------------------------------------ #

    async def _nonterminal(
        self,
        probe_id: str,
        kind: enums.TaskEventKind,
        *,
        message: Optional[str] = None,
        progress: Optional[int] = None,
        returns: Optional[dict] = None,
        level: Optional[str] = None,
    ) -> None:
        recorded = await self.store.record_nonterminal(probe_id, kind.value, returns=returns)
        if recorded is None:
            return  # unknown/expired/already-terminal — a stray report must not raise
        seq, caller, origin = recorded
        await _publish(probe_id, kind.value, seq, message=message, progress=progress, returns=returns, level=level, caller=caller, origin=origin)

    async def on_agent_started(self, agent_id: int, message: messages.Started) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.STARTED)

    async def on_agent_paused(self, agent_id: int, message: messages.Paused) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.PAUSED)

    async def on_agent_resumed(self, agent_id: int, message: messages.Resumed) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.RESUMED)

    async def on_agent_progress(self, agent_id: int, message: messages.Progress) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.PROGRESS, progress=message.progress, message=message.message)

    async def on_agent_log(self, agent_id: int, message: messages.Log) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.LOG, message=message.message, level=message.level)

    async def on_agent_yield(self, agent_id: int, message: messages.Yield) -> None:
        await self._nonterminal(message.task, enums.TaskEventKind.YIELD, returns=message.returns)

    # ------------------------------------------------------------------ #
    # terminal reports
    # ------------------------------------------------------------------ #

    async def _terminal(self, probe_id: str, kind: enums.TaskEventKind, *, error: Optional[str] = None) -> None:
        claimed = await self.store.claim_terminal(probe_id, kind.value, error=error)
        if claimed is None:
            return  # dup terminal resend (agent retries until acked) or expired probe
        seq, state = claimed
        await _publish(probe_id, kind.value, seq, message=error, caller=state.get("caller"), origin=state.get("origin", "graphql"))

    async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None:
        await self._terminal(message.task, enums.TaskEventKind.COMPLETED)

    async def on_agent_cancelled(self, agent_id: int, message: messages.Cancelled) -> None:
        await self._terminal(message.task, enums.TaskEventKind.CANCELLED)

    async def on_agent_interrupted(self, agent_id: int, message: messages.Interrupted) -> None:
        await self._terminal(message.task, enums.TaskEventKind.INTERRUPTED)

    async def on_agent_error(self, agent_id: int, message: messages.Failed) -> None:
        await self._terminal(message.task, enums.TaskEventKind.FAILED, error=message.error)

    async def on_agent_critical(self, agent_id: int, message: messages.Critical) -> None:
        await self._terminal(message.task, enums.TaskEventKind.CRITICAL, error=message.error)

    # ------------------------------------------------------------------ #
    # agent death — probes fail fast (no grace window, no reconcile sweep)
    # ------------------------------------------------------------------ #

    async def fail_all_for_agent(self, agent_pk: int | str) -> int:
        """CRITICAL every live probe of a dead agent, immediately.

        Hover-grade work is worthless after its executor vanished, so unlike tasks
        (grace window + reclaim) probes fail the moment the disconnect is confirmed.
        Winners only — a concurrent sweep or a racing terminal report keeps its claim.
        Returns the number of probes failed.
        """
        probe_ids = await self.store.live_calls_for_agent(agent_pk)
        failed = 0
        for probe_id in probe_ids:
            claimed = await self.store.claim_terminal(probe_id, enums.TaskEventKind.CRITICAL.value, error="Agent disconnected")
            if claimed is None:
                continue
            seq, state = claimed
            await _publish(probe_id, enums.TaskEventKind.CRITICAL.value, seq, message="Agent disconnected", caller=state.get("caller"), origin=state.get("origin", "graphql"))
            failed += 1
        # Only the ids we just looked at (claimed by us, by a racing winner, or expired) —
        # probes registered since then by a reconnected agent stay indexed.
        await self.store.forget_agent_calls(agent_pk, probe_ids)
        return failed


probe_event_backend = ProbeEventBackend()
