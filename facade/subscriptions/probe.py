"""Subscription streaming a probe's events, payload-carrying.

Unlike the task feeds (which relay PKs and re-query per subscriber), probe events carry
their full payload in the channel message — the stream does zero SQL and zero redis
per event. The subscribe race is covered by a snapshot: if events already happened
(``seq > 0``) the current state is emitted first, so a terminal outcome or the latest
yield is never lost to a late subscriber; intermediate events before subscribing are
gone by design (``seq`` gaps make that visible to the client).
"""

import json
from typing import AsyncGenerator

import strawberry
from django.utils import timezone
from kante.types import Info

from facade import enums, models, types
from facade.channels import probe_event_channel
from facade.probes.store import get_probe_store


async def probe_events(
    self: object,
    info: Info,
    probe: strawberry.ID,
) -> AsyncGenerator[types.ProbeEvent, None]:
    """Stream the events of one probe (caller-scoped)."""
    probe_id = str(probe)
    state = await get_probe_store().aget(probe_id)
    if state is None:
        raise ValueError(f"Unknown or expired probe {probe_id}")

    caller, _ = await models.Caller.objects.aget_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
    )
    if state.caller != str(caller.pk):
        raise PermissionError("Not authorized to watch this probe (not its caller).")

    if state.seq > 0:
        # Snapshot: the latest state as a synthetic event, so terminal outcomes and the
        # last yield survive the subscribe race. Full replay is deliberately out of scope.
        yield types.ProbeEvent(
            probe=strawberry.ID(probe_id),
            kind=enums.TaskEventKind(state.kind),
            seq=state.seq,
            message=state.err or None,
            progress=None,
            returns=json.loads(state.last_returns) if state.last_returns else None,
            created_at=timezone.now(),
        )

    async for message in probe_event_channel.listen(info, [f"probe_events_{probe_id}"]):
        yield types.ProbeEvent.from_broadcast(message)
