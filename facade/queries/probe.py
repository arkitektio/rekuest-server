"""Queries for probes' redis-held state."""

import strawberry
from kante.types import Info

from facade import models, types
from facade.caller_context import CallerContext
from facade.probes.store import get_probe_store


def probe(info: Info, id: strawberry.ID) -> types.Probe:
    """Fetch a live (or lingering) probe by id. Org-scoped; expired probes are gone."""
    state = get_probe_store().get(str(id))
    if state is None:
        raise ValueError(f"Unknown or expired probe {id}")
    organization = CallerContext.from_info(info).organization
    if organization is None or state.org != organization.slug:
        raise PermissionError("Not authorized to view this probe.")
    return types.Probe.from_state(state)


def probe_stats(info: Info) -> types.ProbeStats:
    """Live probe counts: instance-wide total plus the requesting caller's inflight/cap."""
    request = info.context.request
    caller, _ = models.Caller.objects.get_or_create(
        client=request.client,
        user=request.user,
        organization=request.organization,
    )
    stats = get_probe_store().stats_sync(caller.pk)
    return types.ProbeStats(total_live=stats.total_live, my_inflight=stats.my_inflight, max_inflight=stats.max_inflight)
