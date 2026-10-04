"""GraphQL types for probes.

Plain strawberry types (NOT strawberry_django): a probe has no model — these are built
from the redis state hash / the payload-carrying channel broadcasts.
"""

from __future__ import annotations

import datetime
import json
from typing import Optional

import strawberry

from facade import enums
from facade.channel_events import ProbeEventBroadcast
from facade.takt_api import ProbeState
from rekuest_core import scalars as rscalars


@strawberry.type(description="A probe — a zero-persistence invocation. Redis-held under a TTL; never appears in task history.")
class Probe:
    id: strawberry.ID
    agent: strawberry.ID = strawberry.field(description="The agent executing this probe.")
    action: strawberry.ID = strawberry.field(description="The called action.")
    implementation: strawberry.ID = strawberry.field(description="The resolved implementation.")
    interface: str = strawberry.field(description="The implementation interface the agent runs.")
    reference: Optional[str] = strawberry.field(description="The caller-side reference, if any.")
    kind: enums.TaskEventKind = strawberry.field(description="Kind of the latest event.")
    seq: int = strawberry.field(description="Per-probe monotonic sequence of the latest event.")
    is_done: bool = strawberry.field(description="Whether the probe reached a terminal state.")
    returns: Optional[rscalars.AnyDefault] = strawberry.field(description="The latest YIELD payload, if any.")
    error: Optional[str] = strawberry.field(description="The terminal error, if the probe failed.")
    created_at: Optional[datetime.datetime] = strawberry.field(description="When the probe was created.")

    @classmethod
    def from_state(cls, state: ProbeState) -> "Probe":
        """Build from the probe's state: the redis hash takt keeps, with its id."""
        return cls(
            id=strawberry.ID(state.id),
            agent=strawberry.ID(state.agent),
            action=strawberry.ID(state.action),
            implementation=strawberry.ID(state.impl),
            interface=state.iface,
            reference=state.ref or None,
            kind=enums.TaskEventKind(state.kind),
            seq=state.seq,
            is_done=bool(state.done),
            returns=json.loads(state.last_returns) if state.last_returns else None,
            error=state.err or None,
            created_at=datetime.datetime.fromisoformat(state.created) if state.created else None,
        )


@strawberry.type(description="A single event of a probe, relayed payload-carrying (no lookups). `seq` orders the stream.")
class ProbeEvent:
    probe: strawberry.ID
    kind: enums.TaskEventKind
    seq: int
    message: Optional[str]
    progress: Optional[int]
    returns: Optional[rscalars.AnyDefault]
    created_at: datetime.datetime

    @classmethod
    def from_broadcast(cls, b: ProbeEventBroadcast) -> "ProbeEvent":
        return cls(
            probe=strawberry.ID(b.probe),
            kind=enums.TaskEventKind(b.kind),
            seq=b.seq,
            message=b.message,
            progress=b.progress,
            returns=rscalars.AnyDefault(b.returns) if b.returns is not None else None,
            created_at=b.created_at,
        )


@strawberry.type(description="Live probe counts from redis — probes have no rows, so stats come from the TTL keyspace.")
class ProbeStats:
    total_live: int = strawberry.field(description="Live (non-expired) probes across the whole instance — a bare count, not scoped.")
    my_inflight: int = strawberry.field(description="The requesting caller's in-flight probes.")
    max_inflight: int = strawberry.field(description="The per-caller in-flight cap (PROBE_MAX_INFLIGHT_PER_CALLER).")
