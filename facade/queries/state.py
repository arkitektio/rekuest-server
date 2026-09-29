from typing import Optional, List, Dict, Any
import datetime

import jsonpatch  # type: ignore[import-untyped]
import strawberry
from django.db.models import Min, Max, QuerySet
from django.core.exceptions import ObjectDoesNotExist

from facade import models, types, inputs, managers
from rekuest_core.inputs import types as ritypes
from kante.types import Info
from facade.logic import get_latest_state


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _get_boundary_aggregates(queryset: QuerySet) -> Optional[Dict[str, Any]]:
    """Helper to extract common start/end aggregations for boundary calculations.

    ``global_rev`` is the revision *after* a patch applies, so the range a patch set spans runs
    from ``min(global_rev) - 1`` (what the first patch applied to) to ``max(global_rev)``.
    """
    agg = queryset.aggregate(
        first_rev=Min("global_rev"),
        last_rev=Max("global_rev"),
        start_time=Min("timestamp"),
        end_time=Max("timestamp"),
    )
    if agg["first_rev"] is None:
        return None
    return {
        "start_global_revision": agg["first_rev"] - 1,
        "end_global_revision": agg["last_rev"],
        "start_time": agg["start_time"],
        "end_time": agg["end_time"],
    }


def _get_state_ids(session_id: str | None, organization) -> List[str]:
    """Get distinct state_ids from both Snapshots and Patches, within one organization.

    Unscoped, and with no ``session_id``, this enumerated every Snapshot and Patch in the
    deployment.
    """
    snapshot_qs = models.Snapshot.objects.filter(agent__organization=organization)
    patch_qs = models.Patch.objects.filter(agent__organization=organization)

    if session_id:
        snapshot_qs = snapshot_qs.filter(session__session_id=session_id)
        patch_qs = patch_qs.filter(session__session_id=session_id)

    snapshot_ids = set(snapshot_qs.values_list("state_id", flat=True))
    patch_ids = set(patch_qs.values_list("state_id", flat=True))
    return sorted(list(snapshot_ids.union(patch_ids)))


def _get_state_at_revision(
    target_revision: int,
    state_id: str,
    session_id: str | None,
) -> Optional[types.Snapshot]:
    """Calculates the state of a specific document at a given revision by applying JSON patches."""
    # 1. Fetch the closest anchor snapshot prior to or at the target revision
    snapshot_qs = models.Snapshot.objects.filter(state_id=state_id)
    if session_id:
        snapshot_qs = snapshot_qs.filter(session__session_id=session_id)

    anchor_snapshot = snapshot_qs.filter(global_rev__lte=target_revision).order_by("-global_rev").first()

    if not anchor_snapshot:
        return None

    # 2. Fetch all patches between the anchor snapshot and the target revision
    patch_start_rev = anchor_snapshot.global_rev or 0
    patch_qs = models.Patch.objects.filter(state_id=state_id)
    if session_id:
        patch_qs = patch_qs.filter(session__session_id=session_id)

    # Patches strictly after the anchor, up to and including the target revision.
    patch_qs = patch_qs.filter(global_rev__gt=patch_start_rev, global_rev__lte=target_revision).order_by("global_rev")

    # 3. Batch apply JSON patches
    state_data = anchor_snapshot.value
    last_snapshot = anchor_snapshot
    patch_docs = []
    last_patch = None

    for patch in patch_qs:
        patch_doc = {"op": patch.op, "path": patch.path}
        if patch.op != "remove":
            patch_doc["value"] = patch.value
        patch_docs.append(patch_doc)
        last_patch = patch

    if patch_docs:
        state_data = jsonpatch.apply_patch(state_data, patch_docs, in_place=False)
        last_snapshot = models.Snapshot(
            value=state_data,
            timestamp=last_patch.timestamp,
            global_rev=last_patch.global_rev,
            session=last_patch.session,
            state=last_patch.state,
            agent=last_patch.agent,
        )

    return last_snapshot


# -----------------------------------------------------------------------------
# GraphQL Resolvers
# -----------------------------------------------------------------------------


def state_for(
    info: Info,
    agent: strawberry.ID,
    state_hash: str | None = None,
    demand: ritypes.StateDemandInput | None = None,
) -> types.State:
    """Get a state for an agent."""
    agent_inst = models.Agent.objects.get(id=agent)

    if demand:
        return models.State.objects.get(agent=agent_inst, **managers.state_demand_state_filters(demand))

    if state_hash:
        return models.State.objects.get(agent=agent_inst, definition__hash=state_hash)

    raise ValueError("Either state_hash or a valid demand must be provided")


def task_boundaries(
    info: Info,
    correlation_id: str,
    state_id: strawberry.ID | None = None,
) -> Optional[types.TaskBoundary]:
    """Retrieve min/max revisions and times for a task correlation ID."""
    queryset = models.Patch.objects.filter(task__reference=correlation_id, agent__organization=info.context.request.organization)
    if state_id:
        queryset = queryset.filter(state_id=state_id)

    agg = _get_boundary_aggregates(queryset)
    if not agg:
        return None

    return types.TaskBoundary(correlation_id=correlation_id, **agg)


def session_boundaries(
    info: Info,
    session_id: strawberry.ID,
    state_id: strawberry.ID | None = None,
) -> Optional[types.SessionBoundary]:
    """Retrieve min/max revisions and times for a specific session."""
    queryset = models.Patch.objects.filter(session__session_id=session_id, agent__organization=info.context.request.organization)
    if state_id:
        queryset = queryset.filter(state_id=state_id)

    agg = _get_boundary_aggregates(queryset)
    if not agg:
        return None

    return types.SessionBoundary(session_id=session_id, **agg)


def state_at_global_rev(
    info: Info,
    global_revision: int,
    state_id: strawberry.ID | None = None,
    session_id: str | None = None,
) -> List[types.Snapshot]:
    state_ids = [str(state_id)] if state_id else _get_state_ids(session_id, info.context.request.organization)

    results = []
    for sid in state_ids:
        res = _get_state_at_revision(global_revision, sid, session_id)
        if res:
            results.append(res)

    return results


def forward_events_after_rev(
    info: Info,
    global_revision: int,
    state_id: strawberry.ID | None = None,
    session_id: str | None = None,
    count: int = 100,
) -> List[types.Patch]:
    # current_revision >= X  <=>  global_rev - 1 >= X  <=>  global_rev > X
    queryset = models.Patch.objects.filter(agent__organization=info.context.request.organization, global_rev__gt=global_revision)
    if state_id:
        queryset = queryset.filter(state_id=state_id)
    if session_id:
        queryset = queryset.filter(session__session_id=session_id)

    return list(queryset.order_by("global_rev", "state_id")[:count])


def patch_events_between_global_revs(
    info: Info,
    from_global_revision: int,
    to_global_revision: int,
    state_ids: List[strawberry.ID] | None = None,
    session_id: str | None = None,
) -> List[types.Patch]:
    if to_global_revision < from_global_revision:
        return []

    queryset = models.Patch.objects.filter(
        agent__organization=info.context.request.organization,
        global_rev__gt=from_global_revision,
        global_rev__lte=to_global_revision,
    )
    if state_ids:
        queryset = queryset.filter(state_id__in=state_ids)
    if session_id:
        queryset = queryset.filter(session__session_id=session_id)

    return list(queryset.order_by("global_rev", "state_id"))


def snapshots_around_rev(
    info: Info,
    revision: int,
    state_id: strawberry.ID | None = None,
    session_id: str | None = None,
    before: int = 1,
    after: int = 1,
) -> List[types.Snapshot]:
    target_state_ids = [str(state_id)] if state_id else _get_state_ids(session_id, info.context.request.organization)
    collected: List[types.Snapshot] = []

    for sid in target_state_ids:
        qs_before = models.Snapshot.objects.filter(state_id=sid, global_rev__lte=revision)
        qs_after = models.Snapshot.objects.filter(state_id=sid, global_rev__gt=revision)

        if session_id:
            qs_before = qs_before.filter(session__session_id=session_id)
            qs_after = qs_after.filter(session__session_id=session_id)

        before_list = list(qs_before.order_by("-global_rev")[:before][::-1])  # Reverse to chronological
        after_list = list(qs_after.order_by("global_rev")[:after])

        collected.extend(before_list + after_list)

    return collected


# -----------------------------------------------------------------------------
# Checkouts & State Values
# -----------------------------------------------------------------------------


@strawberry.type
class StateValue:
    """A state value and its schema."""

    state_id: strawberry.ID = strawberry.field(description="The ID of the state")
    value: strawberry.scalars.JSON = strawberry.field(description="The state value")
    global_revision: int | None = strawberry.field(description="The global revision of this state")
    forward_patches: List[types.Patch] = strawberry.field(description="The patches that can be applied to move forward from this state")
    backward_patches: List[types.Patch] = strawberry.field(description="The patches that can be applied to move backward from this state")


def checkout(
    info: Info,
    state: strawberry.ID,
    session_id: strawberry.ID | None = None,
    global_revision: int | None = None,
    timestamp: datetime.datetime | None = None,
    forward_patch_count: int = 0,
    backward_patch_count: int = 0,
) -> StateValue:
    """Checkout a state at a specific revision."""
    state_inst = models.State.objects.get(id=state)

    if timestamp:
        latest_patch = models.Patch.objects.filter(state_id=state, agent=state_inst.agent, timestamp__lte=timestamp).order_by("-timestamp").first()

        if not latest_patch:
            raise ObjectDoesNotExist(f"No patches found for state {state} before timestamp {timestamp}")

        target_global_rev = latest_patch.global_rev
        # The session *identifier*: get_latest_state looks the session up by ``session_id``, and
        # passing the row pk here resolved to no session (or a stranger's) for every timestamp.
        target_session = latest_patch.session.session_id if latest_patch.session else None
    else:
        target_global_rev = global_revision
        target_session = session_id

    result_payload = get_latest_state(
        state_inst.agent,
        state_id=state_inst.pk,
        global_revision=target_global_rev,
        session_id=target_session,
        forward_patch_count=forward_patch_count,
        backward_patch_count=backward_patch_count,
    )

    if not result_payload or "states" not in result_payload or state_inst.interface not in result_payload["states"]:
        raise ObjectDoesNotExist(f"No state found for {state}")

    state_data = result_payload["states"][state_inst.interface]
    # The revision lives on the payload, not inside the state's value (which is the user's
    # document and never has a ``global_revision`` key). Scoped to this one state above, so the
    # payload's maximum is this state's revision.
    current_global_rev = result_payload.get("global_revision")

    # Forward patches are now fetched by get_latest_state at the global agent level
    forward_patches = result_payload.get("forward_patches", [])

    # Backward patches are now fetched by get_latest_state at the global agent level
    backward_patches = result_payload.get("backward_patches", [])

    return StateValue(
        state_id=state,
        value=state_data,
        global_revision=current_global_rev,
        forward_patches=forward_patches,
        backward_patches=backward_patches,
    )


@strawberry.type
class AgentWithValues:
    """An agent alongside all of its current state values."""

    agent_id: strawberry.ID = strawberry.field(description="The ID of the agent this state belongs to")
    values: strawberry.scalars.JSON = strawberry.field(description="The state value, indexed by state_interface")
    global_revision: int = strawberry.field(description="The maximum global revision across these states")
    forward_patches: List[types.Patch] = strawberry.field(description="The patches to move forward")
    backward_patches: List[types.Patch] = strawberry.field(description="The patches to move backward")


def checkout_agent(
    info: Info,
    agent: strawberry.ID,
    session_id: strawberry.ID | None = None,
    global_revision: int | None = None,
    timestamp: datetime.datetime | None = None,
    forward_patch_count: int = 0,
    backward_patch_count: int = 0,
) -> AgentWithValues:
    """Checkout all states for a given agent at a specific revision."""
    agent_inst = models.Agent.objects.get(id=agent)

    if timestamp:
        latest_patch = models.Patch.objects.filter(agent=agent, timestamp__lte=timestamp).order_by("-timestamp").first()

        if not latest_patch:
            raise ObjectDoesNotExist(f"No patches found for state {agent} before timestamp {timestamp}")

        target_global_rev = latest_patch.global_rev
        target_session = latest_patch.session.session_id if latest_patch.session else None
    else:
        target_global_rev = global_revision
        target_session = session_id

    result_payload = (
        get_latest_state(
            agent_inst,
            global_revision=target_global_rev,
            session_id=target_session,
            forward_patch_count=forward_patch_count,
            backward_patch_count=backward_patch_count,
        )
        or {}
    )

    agent_values = {}
    result_map = result_payload.get("states", {})
    max_global_revision = result_payload.get("global_revision", 0)

    # Use the forward patches returned directly from the payload
    forward_patches = result_payload.get("forward_patches", [])

    # Use the backward patches returned directly from the payload
    backward_patches = result_payload.get("backward_patches", [])

    return AgentWithValues(
        agent_id=agent_inst.pk,
        values=result_map,
        global_revision=max_global_revision,
        forward_patches=forward_patches,
        backward_patches=backward_patches,
    )
